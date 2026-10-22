<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { useSessionStore } from '~/stores/session'
import type { SessionItem } from '~/stores/session'
import { useTaskStore } from '~/stores/task'
import type { SubmitTaskPayload } from '~/stores/task'
import { useUiStore } from '~/stores/ui'
import { useWorkflowStore } from '~/stores/workflow'
import {
  BACKEND_GPU_SPECS,
  BACKEND_LABELS,
  BACKEND_TPU_SPECS,
  COMPUTE_BACKENDS,
} from '~/utils/computeBackends'
import type { ComputeBackend } from '~/utils/computeBackends'
import type { WorkflowItem } from '~/stores/workflow'
import {
  parseWorkflowDraftRecord,
  serializeWorkflowDraft,
  workflowDraftStorageKey,
} from '~/utils/workflowDraft'

// /tasks/new 任务提交与工作流管理（operator+，doc/04 §2.1）：
// 字段对齐 Runner POST /tasks 请求体（doc/02 §2.3），并把
// validate_workflow_accelerator()（run.py:1105）的规则搬到前端做前置校验。
const route = useRoute()
const activeTab = computed(() => route.query.tab === 'workflows' ? 'workflows' : 'submit')
useHead({ title: () => activeTab.value === 'workflows' ? '工作流与模型' : '提交任务' })

const authStore = useAuthStore()
const workflowStore = useWorkflowStore()
const sessionStore = useSessionStore()
const taskStore = useTaskStore()
const uiStore = useUiStore()
const apiFetch = useNuxtApp().$apiFetch

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

const SESSION_NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/

// ── 表单字段（与 doc/02 §2.3 请求体一一对应）─────────────────────
const workflowConfig = ref('')
const sessionName = ref('comfyui-h3')
const backend = ref<ComputeBackend>('colab')
const acceleratorKind = ref<'gpu' | 'tpu'>('gpu')
const acceleratorSpec = ref('T4')
const mountDrive = ref(true)
const reuse = ref(false)
const keep = ref(false)
const timeoutSeconds = ref(86400)
const dryRun = ref(false)
const workflowFieldValues = ref<Record<string, string>>({})
const workflowFieldDefaults = ref<Record<string, string>>({})
const workflowDraftRestored = ref(false)
const workflowDraftStatus = ref<'idle' | 'loading' | 'saving' | 'saved' | 'error'>('idle')
const workflowDraftErrorMessage = ref('')
const workflowDraftResetting = ref(false)
const workflowImageUrls = ref<Record<string, string>>({})
const workflowImageDimensions = ref<Record<string, { width: number, height: number }>>({})
const workflowImageFailures = ref<Set<string>>(new Set())
const workflowImagesLoading = ref(false)
const firstFrameFile = ref<File | null>(null)
const lastFrameFile = ref<File | null>(null)
const uploadingFrames = ref(false)
const frameUploadError = ref('')
const inputImageFiles = ref<Record<string, File | null>>({})
let workflowImageRequestId = 0
let hydratingWorkflowFields = false
let workflowDraftLoadId = 0

interface WorkflowDraftContext {
  userId: number
  slug: string
  key: string
}

interface WorkflowDraftResponse {
  draft: { values: Record<string, string>, updatedAt: string } | null
}

const workflowDraftSaveTimers = new Map<string, ReturnType<typeof setTimeout>>()
const workflowDraftSaveChains = new Map<string, Promise<boolean>>()
const workflowDraftVersions = new Map<string, number>()
const workflowDraftPersistedVersions = new Map<string, number>()

// validate() 返回 string | null（null = 通过），这里跟着一起用 null 表示无错
const errorMessage = ref<string | null>(null)
const submitting = ref(false)

const selectedWorkflow = computed(() =>
  workflowStore.workflows.find(w => w.configPath === workflowConfig.value) ?? null)
const autoAspectEnabled = computed(() =>
  selectedWorkflow.value?.slug === 'minimax_h3'
  || selectedWorkflow.value?.slug === 'minimax_h3_first_frame')

function currentWorkflowDraftContext(): WorkflowDraftContext | null {
  const userId = authStore.user?.id
  const slug = selectedWorkflow.value?.slug
  if (!userId || !slug) return null
  return { userId, slug, key: workflowDraftStorageKey(userId, slug) }
}

function isCurrentWorkflowDraft(context: WorkflowDraftContext): boolean {
  const current = currentWorkflowDraftContext()
  return current?.userId === context.userId && current.slug === context.slug
}

function hydrateWorkflowFields(values: Record<string, string>) {
  hydratingWorkflowFields = true
  workflowFieldValues.value = values
  hydratingWorkflowFields = false
}

function localWorkflowDraft(context: WorkflowDraftContext, allowedKeys: string[]) {
  if (!import.meta.client) return null
  try {
    return parseWorkflowDraftRecord(localStorage.getItem(context.key), allowedKeys)
  } catch {
    return null
  }
}

function cacheWorkflowDraftLocally(context: WorkflowDraftContext, values: Record<string, string>) {
  if (!import.meta.client) return
  try {
    localStorage.setItem(context.key, serializeWorkflowDraft(values))
  } catch {
    // 数据库仍是主存储；本地缓存仅用于网络失败兜底和旧草稿迁移。
  }
}

function removeLocalWorkflowDraft(context: WorkflowDraftContext) {
  if (!import.meta.client) return
  try {
    localStorage.removeItem(context.key)
  } catch {
    // 浏览器禁止本地存储不影响数据库草稿。
  }
}

async function persistWorkflowDraft(
  context: WorkflowDraftContext,
  values: Record<string, string>,
  version: number,
): Promise<boolean> {
  try {
    await apiFetch(`/api/workflows/${encodeURIComponent(context.slug)}/draft`, {
      method: 'PUT',
      body: { values },
    })
    workflowDraftPersistedVersions.set(
      context.key,
      Math.max(workflowDraftPersistedVersions.get(context.key) ?? 0, version),
    )
    if ((workflowDraftVersions.get(context.key) ?? 0) === version) {
      removeLocalWorkflowDraft(context)
    }
    if (isCurrentWorkflowDraft(context)
      && (workflowDraftPersistedVersions.get(context.key) ?? 0)
      >= (workflowDraftVersions.get(context.key) ?? 0)) {
      workflowDraftStatus.value = 'saved'
      workflowDraftErrorMessage.value = ''
    }
    return true
  } catch (error) {
    if (isCurrentWorkflowDraft(context)) {
      workflowDraftStatus.value = 'error'
      workflowDraftErrorMessage.value = extractApiError(error, '数据库草稿保存失败')
    }
    return false
  }
}

function enqueueWorkflowDraftSave(
  context: WorkflowDraftContext,
  values: Record<string, string>,
  version: number,
): Promise<boolean> {
  const previous = workflowDraftSaveChains.get(context.key) ?? Promise.resolve(true)
  const next = previous.then(() => persistWorkflowDraft(context, values, version))
  workflowDraftSaveChains.set(context.key, next)
  void next.finally(() => {
    if (workflowDraftSaveChains.get(context.key) === next) {
      workflowDraftSaveChains.delete(context.key)
    }
  })
  return next
}

function scheduleWorkflowDraftSave(
  context: WorkflowDraftContext,
  values: Record<string, string>,
  version: number,
) {
  const oldTimer = workflowDraftSaveTimers.get(context.key)
  if (oldTimer) clearTimeout(oldTimer)
  workflowDraftSaveTimers.set(context.key, setTimeout(() => {
    workflowDraftSaveTimers.delete(context.key)
    void enqueueWorkflowDraftSave(context, values, version)
  }, 500))
}

async function flushCurrentWorkflowDraft(): Promise<boolean> {
  const context = currentWorkflowDraftContext()
  if (!context || selectedWorkflow.value?.editableFields.length === 0) return true
  const version = workflowDraftVersions.get(context.key) ?? 0
  if (version <= (workflowDraftPersistedVersions.get(context.key) ?? 0)) return true

  const timer = workflowDraftSaveTimers.get(context.key)
  if (timer) {
    clearTimeout(timer)
    workflowDraftSaveTimers.delete(context.key)
  }
  workflowDraftStatus.value = 'saving'
  return await enqueueWorkflowDraftSave(context, { ...workflowFieldValues.value }, version)
}

watch([() => selectedWorkflow.value?.slug, () => authStore.user?.id], async ([, userId]) => {
  const workflow = selectedWorkflow.value
  const loadId = ++workflowDraftLoadId
  const defaults: Record<string, string> = {}
  for (const field of workflow?.editableFields ?? []) {
    defaults[field.key] = field.defaultValue ?? ''
  }
  workflowFieldDefaults.value = defaults
  hydrateWorkflowFields({ ...defaults })
  workflowDraftRestored.value = false
  workflowDraftErrorMessage.value = ''
  if (!workflow || !userId) {
    workflowDraftStatus.value = 'idle'
    return
  }

  const context: WorkflowDraftContext = {
    userId,
    slug: workflow.slug,
    key: workflowDraftStorageKey(userId, workflow.slug),
  }
  const allowedKeys = workflow.editableFields.map(field => field.key)
  const localDraft = localWorkflowDraft(context, allowedKeys)
  if (!workflowDraftVersions.has(context.key)) workflowDraftVersions.set(context.key, 0)
  if (!workflowDraftPersistedVersions.has(context.key)) {
    workflowDraftPersistedVersions.set(context.key, 0)
  }
  workflowDraftStatus.value = 'loading'

  try {
    const response = await apiFetch<WorkflowDraftResponse>(
      `/api/workflows/${encodeURIComponent(workflow.slug)}/draft`,
    )
    if (loadId !== workflowDraftLoadId) return

    const serverDraft = response.draft
    const localIsNewer = Boolean(
      localDraft
      && (
        !serverDraft
        || (localDraft.updatedAt !== null
          && Date.parse(localDraft.updatedAt) > Date.parse(serverDraft.updatedAt))
      ),
    )
    const restoredValues = localIsNewer ? localDraft!.values : (serverDraft?.values ?? {})
    hydrateWorkflowFields({ ...defaults, ...restoredValues })
    workflowDraftRestored.value = Object.keys(restoredValues).length > 0

    if (localIsNewer) {
      const migrationVersion = (workflowDraftVersions.get(context.key) ?? 0) + 1
      workflowDraftVersions.set(context.key, migrationVersion)
      workflowDraftStatus.value = 'saving'
      await enqueueWorkflowDraftSave(
        context,
        { ...workflowFieldValues.value },
        migrationVersion,
      )
    } else {
      removeLocalWorkflowDraft(context)
      workflowDraftStatus.value = serverDraft ? 'saved' : 'idle'
    }
  } catch (error) {
    if (loadId !== workflowDraftLoadId) return
    if (localDraft) {
      hydrateWorkflowFields({ ...defaults, ...localDraft.values })
      workflowDraftRestored.value = Object.keys(localDraft.values).length > 0
    }
    workflowDraftStatus.value = 'error'
    workflowDraftErrorMessage.value = extractApiError(error, '数据库草稿读取失败')
  }
})

watch(workflowFieldValues, (values) => {
  if (!import.meta.client || hydratingWorkflowFields) return
  const context = currentWorkflowDraftContext()
  if (!context) return
  const version = (workflowDraftVersions.get(context.key) ?? 0) + 1
  workflowDraftVersions.set(context.key, version)
  cacheWorkflowDraftLocally(context, values)
  workflowDraftStatus.value = 'saving'
  workflowDraftErrorMessage.value = ''
  scheduleWorkflowDraftSave(context, { ...values }, version)
}, { deep: true, flush: 'sync' })

async function resetWorkflowFields() {
  if (workflowDraftResetting.value) return
  const context = currentWorkflowDraftContext()
  hydrateWorkflowFields({ ...workflowFieldDefaults.value })
  workflowDraftRestored.value = false
  if (!context) return

  const timer = workflowDraftSaveTimers.get(context.key)
  if (timer) {
    clearTimeout(timer)
    workflowDraftSaveTimers.delete(context.key)
  }
  removeLocalWorkflowDraft(context)
  workflowDraftResetting.value = true
  workflowDraftStatus.value = 'saving'
  const resetVersion = (workflowDraftVersions.get(context.key) ?? 0) + 1
  workflowDraftVersions.set(context.key, resetVersion)

  const previous = workflowDraftSaveChains.get(context.key) ?? Promise.resolve(true)
  const reset = previous.then(async () => {
    try {
      await apiFetch(`/api/workflows/${encodeURIComponent(context.slug)}/draft`, {
        method: 'DELETE',
      })
      workflowDraftPersistedVersions.set(context.key, resetVersion)
      if (isCurrentWorkflowDraft(context)) {
        workflowDraftStatus.value = 'idle'
        workflowDraftErrorMessage.value = ''
        uiStore.info('已删除数据库草稿并恢复工作流默认参数')
      }
      return true
    } catch (error) {
      if (isCurrentWorkflowDraft(context)) {
        workflowDraftStatus.value = 'error'
        workflowDraftErrorMessage.value = extractApiError(error, '删除数据库草稿失败')
        uiStore.error(workflowDraftErrorMessage.value)
      }
      return false
    } finally {
      workflowDraftResetting.value = false
    }
  })
  workflowDraftSaveChains.set(context.key, reset)
  await reset
}

function releaseWorkflowImageUrls() {
  for (const url of Object.values(workflowImageUrls.value)) URL.revokeObjectURL(url)
  workflowImageUrls.value = {}
}

async function loadWorkflowImages(workflow: WorkflowItem | null) {
  const requestId = ++workflowImageRequestId
  releaseWorkflowImageUrls()
  workflowImageDimensions.value = {}
  workflowImageFailures.value = new Set()
  const images = (workflow?.inputImages ?? []).filter(image => image.available && image.url)
  if (images.length === 0) {
    workflowImagesLoading.value = false
    return
  }

  workflowImagesLoading.value = true
  const nextUrls: Record<string, string> = {}
  const failures = new Set<string>()
  const { $apiFetch } = useNuxtApp()
  await Promise.all(images.map(async (image) => {
    try {
      const blob = await $apiFetch<Blob>(image.url!, { responseType: 'blob' })
      const objectUrl = URL.createObjectURL(blob)
      if (requestId !== workflowImageRequestId) {
        URL.revokeObjectURL(objectUrl)
      } else {
        nextUrls[image.nodeId] = objectUrl
      }
    } catch {
      failures.add(image.nodeId)
    }
  }))
  if (requestId === workflowImageRequestId) {
    workflowImageUrls.value = nextUrls
    workflowImageFailures.value = failures
    workflowImagesLoading.value = false
  }
}

watch(() => selectedWorkflow.value?.slug, () => {
  const workflow = selectedWorkflow.value
  firstFrameFile.value = null
  lastFrameFile.value = null
  inputImageFiles.value = {}
  frameUploadError.value = ''
  void loadWorkflowImages(workflow)
})

async function refreshWorkflowImages(workflow: WorkflowItem) {
  const response = await apiFetch<{ items: WorkflowItem[] }>('/api/workflows')
  const refreshed = response.items.find(item => item.slug === workflow.slug)
  if (refreshed && selectedWorkflow.value?.slug === workflow.slug) {
    workflow.inputImages = refreshed.inputImages
    await loadWorkflowImages(workflow)
  }
}

async function uploadFrames() {
  const workflow = selectedWorkflow.value
  if (!workflow || (!firstFrameFile.value && !lastFrameFile.value)) return
  uploadingFrames.value = true
  frameUploadError.value = ''
  try {
    const formData = new FormData()
    if (firstFrameFile.value) formData.append('first', firstFrameFile.value)
    if (lastFrameFile.value) formData.append('last', lastFrameFile.value)
    await apiFetch(`/api/workflows/${encodeURIComponent(workflow.slug)}/frames`, {
      method: 'POST',
      body: formData,
    })
    firstFrameFile.value = null
    lastFrameFile.value = null
    await refreshWorkflowImages(workflow)
    uiStore.info('输入图片已更新')
  } catch (error) {
    frameUploadError.value = extractApiError(error, '上传图片失败')
    throw error
  } finally {
    uploadingFrames.value = false
  }
}

async function uploadInputImages() {
  const workflow = selectedWorkflow.value
  if (!workflow) return
  const files = Object.entries(inputImageFiles.value).filter((entry): entry is [string, File] => Boolean(entry[1]))
  if (files.length === 0) return
  uploadingFrames.value = true
  frameUploadError.value = ''
  try {
    for (const [nodeId, file] of files) {
      const formData = new FormData()
      formData.append('image', file)
      await apiFetch(`/api/workflows/${encodeURIComponent(workflow.slug)}/images/${encodeURIComponent(nodeId)}`, {
        method: 'POST',
        body: formData,
      })
      inputImageFiles.value[nodeId] = null
    }
    await refreshWorkflowImages(workflow)
    uiStore.info('输入图片已更新')
  } catch (error) {
    frameUploadError.value = extractApiError(error, '上传图片失败')
    await refreshWorkflowImages(workflow).catch(() => {})
    throw error
  } finally {
    uploadingFrames.value = false
  }
}

async function onUploadInputImages() {
  try {
    await uploadInputImages()
  } catch {
    // 错误显示在图片区域，未上传的文件仍可重试。
  }
}

async function onUploadFrames() {
  try {
    await uploadFrames()
  } catch {
    // 错误显示在图片区域，保留已选文件以便重试。
  }
}

function onWorkflowImageLoad(nodeId: string, event: Event) {
  const image = event.currentTarget as HTMLImageElement
  if (image.naturalWidth <= 0 || image.naturalHeight <= 0) return
  workflowImageDimensions.value = {
    ...workflowImageDimensions.value,
    [nodeId]: { width: image.naturalWidth, height: image.naturalHeight },
  }
}

onBeforeUnmount(() => {
  workflowImageRequestId += 1
  releaseWorkflowImageUrls()
  void flushCurrentWorkflowDraft()
})

const specOptions = computed(() =>
  (acceleratorKind.value === 'gpu' ? BACKEND_GPU_SPECS : BACKEND_TPU_SPECS)[backend.value])

watch([acceleratorKind, backend], () => {
  if (BACKEND_TPU_SPECS[backend.value].length === 0) acceleratorKind.value = 'gpu'
  if (!specOptions.value.includes(acceleratorSpec.value)) {
    acceleratorSpec.value = specOptions.value[0] ?? 'T4'
  }
})

/** doc/04 §2.1：supportedAccelerators 之外的加速器置灰，title 显示 acceleratorNote */
function acceleratorSupported(kind: 'gpu' | 'tpu'): boolean {
  if (kind === 'tpu' && BACKEND_TPU_SPECS[backend.value].length === 0) return false
  const wf = selectedWorkflow.value
  if (!wf || wf.supportedAccelerators.length === 0) return true
  return wf.supportedAccelerators.includes(kind)
}

function acceleratorDisabledTitle(kind: 'gpu' | 'tpu'): string {
  if (acceleratorSupported(kind)) return ''
  if (kind === 'tpu' && BACKEND_TPU_SPECS[backend.value].length === 0) return `${BACKEND_LABELS[backend.value]} 后端不支持 TPU`
  return selectedWorkflow.value?.acceleratorNote
    ?? `工作流 ${selectedWorkflow.value?.name ?? ''} 不支持 ${kind.toUpperCase()}`
}

/** 模型总体积与「首次运行需下载 ~N GiB」提示（doc/04 §2.1） */
const totalModelGiB = computed(() => {
  const bytes = Number(selectedWorkflow.value?.totalModelBytes ?? 0)
  return Number.isFinite(bytes) && bytes > 0 ? bytes / 1024 ** 3 : null
})

// ── 复用会话：隐藏加速器（由现有 VM 决定），实时确认目标会话非 busy ──
watch(reuse, async on => {
  if (!on) return
  try {
    await sessionStore.fetchSessions()
  } catch (err) {
    uiStore.error(extractApiError(err, '拉取会话列表失败'))
  }
})

const reuseTarget = computed(() =>
  sessionStore.sessions.find(s => s.name === sessionName.value) ?? null)

// P4 之后 SessionItem 没有扁平的 status 字段，拆成了 runnerStatus（Colab 实况）
// 与 dbStatus（对账结果）；「忙」的判定与 /sessions 页 statusText 保持一致。
function reuseStatusText(target: SessionItem): string {
  if (target.reachable) return target.runnerStatus === 'BUSY' ? '运行中' : '空闲'
  if (target.dbStatus === 'unreachable') return '失联'
  if (target.dbStatus === 'stopped') return '已停止'
  return '不在线'
}

const reuseProblem = computed<string | null>(() => {
  if (!reuse.value) return null
  const target = reuseTarget.value
  if (!target) return '未找到同名会话，提交时 Runner 将按规则校验'
  if (target.reachable && target.runnerStatus === 'BUSY') {
    return `会话 ${sessionName.value} 正忙（BUSY），同一会话同时只能跑一个任务`
  }
  if (target.hasRunningTask) {
    return `会话 ${sessionName.value} 已有在途任务，同一会话同时只能跑一个任务`
  }
  return null
})

function validate(): string | null {
  if (!workflowConfig.value) return '请选择工作流'
  if (!SESSION_NAME_PATTERN.test(sessionName.value)) {
    return '会话名仅允许字母、数字、_ . -，最长 128 字符'
  }
  if (!reuse.value && !acceleratorSupported(acceleratorKind.value)) {
    return acceleratorDisabledTitle(acceleratorKind.value)
  }
  if (reuse.value && reuseProblem.value) return reuseProblem.value
  if (!Number.isFinite(timeoutSeconds.value) || timeoutSeconds.value <= 0) {
    return '超时时间必须是正数（秒）'
  }
  for (const field of selectedWorkflow.value?.editableFields ?? []) {
    if (field.required && !(workflowFieldValues.value[field.key] ?? '').trim()) {
      return `${field.label}不能为空`
    }
  }
  return null
}

function changedWorkflowOverrides(): Record<string, string> {
  const overrides: Record<string, string> = {}
  for (const field of selectedWorkflow.value?.editableFields ?? []) {
    const value = workflowFieldValues.value[field.key] ?? ''
    if (value !== (workflowFieldDefaults.value[field.key] ?? '')) {
      overrides[field.key] = value
    }
  }
  return overrides
}

async function onSubmit() {
  errorMessage.value = validate()
  if (errorMessage.value) return
  submitting.value = true
  try {
    const draftSaved = await flushCurrentWorkflowDraft()
    if (!draftSaved) {
      uiStore.error('数据库草稿保存失败；本次任务仍会使用当前参数提交')
    }
    if (firstFrameFile.value || lastFrameFile.value) await uploadFrames()
    if (Object.values(inputImageFiles.value).some(Boolean)) await uploadInputImages()
    const payload: SubmitTaskPayload = {
      workflow_config: workflowConfig.value,
      api_json: null,
      session: sessionName.value.trim(),
      accelerator: { kind: acceleratorKind.value, spec: acceleratorSpec.value },
      backend: backend.value,
      mount_drive: mountDrive.value,
      reuse: reuse.value,
      keep: keep.value,
      input_dir: null,
      workflow_overrides: changedWorkflowOverrides(),
      timeout: timeoutSeconds.value,
      ...(dryRun.value ? { dry_run: true } : {}),
    }
    const taskId = await taskStore.submitTask(payload)
    uiStore.info(`任务已受理：${taskId}`)
    await navigateTo(`/tasks/${taskId}`)
  } catch (err) {
    errorMessage.value = extractApiError(err, '提交失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

async function onSync() {
  try {
    const res = await workflowStore.sync()
    uiStore.info(`已同步 ${res.synced} 个工作流：${res.slugs.join('、')}`)
  } catch (err) {
    uiStore.error(extractApiError(err, '同步工作流失败'))
  }
}

const formattedTimeout = computed(() => {
  const s = timeoutSeconds.value
  if (!Number.isFinite(s) || s <= 0) return '未设置'
  if (s % 3600 === 0) return `${s / 3600} 小时`
  if (s > 3600) return `${(s / 3600).toFixed(1)} 小时`
  if (s >= 60) return `${Math.round(s / 60)} 分钟`
  return `${s} 秒`
})

const draftStatusLabel = computed(() => {
  if (workflowDraftStatus.value === 'loading') return '读取中…'
  if (workflowDraftStatus.value === 'saving') return '同步中…'
  if (workflowDraftStatus.value === 'error') return '保存异常'
  if (workflowDraftRestored.value) return '已恢复草稿'
  if (workflowDraftStatus.value === 'saved') return '已保存'
  return '准备就绪'
})

onMounted(async () => {
  if (activeTab.value !== 'submit') return
  try {
    await workflowStore.fetchWorkflows()
  } catch (err) {
    uiStore.error(extractApiError(err, '加载工作流列表失败'))
  }
})
</script>

<template>
  <div class="container_task_create_form_root">
    <div class="container_task_create_header_root">
      <div>
        <h1 class="container_task_create_header_title">任务调度与工作流</h1>
        <p class="container_task_create_header_desc">
          配置 ComfyUI 工作流、运行参数与 Colab 算力节点，一键提交执行
        </p>
      </div>
    </div>

    <nav class="container_task_create_tabs_root" aria-label="任务与工作流">
      <NuxtLink
        class="container_task_create_tabs_link"
        :data-active="activeTab === 'submit' ? 'true' : 'false'"
        to="/tasks/new"
      >
        提交任务
      </NuxtLink>
      <NuxtLink
        class="container_task_create_tabs_link"
        :data-active="activeTab === 'workflows' ? 'true' : 'false'"
        to="/tasks/new?tab=workflows"
      >
        工作流与模型
      </NuxtLink>
    </nav>

    <WorkflowManagement v-if="activeTab === 'workflows'" />

    <p v-else-if="!isOperator" class="container_task_create_form_denied" role="alert">
      当前角色为只读，无权提交任务。
    </p>

    <template v-else>
      <p v-if="workflowStore.workflows.length === 0 && !workflowStore.loading" class="container_task_create_form_hint">
        工作流列表为空（首次使用时 workflow_configs 表无数据）。
        <button
          class="container_task_create_toolbar_sync"
          type="button"
          :disabled="workflowStore.syncing"
          @click="onSync"
        >
          {{ workflowStore.syncing ? '同步中…' : '从文件同步工作流' }}
        </button>
      </p>

      <form class="container_task_create_form_body" @submit.prevent="onSubmit">
        <div class="container_task_create_panel_layout">
          <div class="container_task_create_panel_main">
            <!-- 01: 工作流与输入资产 -->
            <section class="container_task_create_card_section">
              <header class="container_task_create_header_section">
                <span class="container_task_create_badge_step">01</span>
                <div>
                  <h2 class="container_task_create_header_sectitle">工作流与输入资产</h2>
                  <p class="container_task_create_header_secdesc">
                    选择要执行的 ComfyUI 工作流，检查模型下载量并配置输入图像
                  </p>
                </div>
              </header>

              <label class="container_task_create_form_workflow">
                <span class="container_task_create_cell_fieldlabel">工作流配置文件</span>
                <select v-model="workflowConfig" required :disabled="submitting || uploadingFrames">
                  <option value="" disabled>请选择工作流</option>
                  <option v-for="wf in workflowStore.workflows" :key="wf.slug" :value="wf.configPath">
                    {{ wf.name }}（{{ wf.slug }}）
                  </option>
                </select>
              </label>

              <div v-if="totalModelGiB !== null" class="container_task_create_form_modelsize">
                <span class="container_task_create_cell_modelsizetag">模型体量 ~{{ totalModelGiB.toFixed(1) }} GiB</span>
                <span>首次运行需下载 ~{{ Math.ceil(totalModelGiB) }} GiB（后续命中 Drive 缓存将直接加速加载）。</span>
              </div>

              <!-- 输入图片区 -->
              <section
                v-if="selectedWorkflow?.inputImages?.length"
                class="container_task_create_form_images"
              >
                <h3 class="container_task_images_header_title">当前输入图片</h3>
                <div class="container_task_images_body_grid">
                  <figure
                    v-for="inputImage in (selectedWorkflow?.inputImages ?? [])"
                    :key="inputImage.nodeId"
                    class="container_task_image_body_card"
                  >
                    <img
                      v-if="workflowImageUrls[inputImage.nodeId]"
                      class="container_task_image_body_preview"
                      :src="workflowImageUrls[inputImage.nodeId]"
                      :alt="`${inputImage.label}：${inputImage.filename}`"
                      @load="onWorkflowImageLoad(inputImage.nodeId, $event)"
                    >
                    <div v-else class="container_task_image_body_empty">
                      <template v-if="!inputImage.available">图片文件不存在，请先上传</template>
                      <template v-else-if="workflowImageFailures.has(inputImage.nodeId)">图片加载失败</template>
                      <template v-else>{{ workflowImagesLoading ? '图片加载中…' : '暂无预览' }}</template>
                    </div>
                    <figcaption class="container_task_image_footer_caption">
                      <strong>{{ inputImage.label }}</strong>
                      <span>{{ inputImage.filename }}</span>
                      <span v-if="workflowImageDimensions[inputImage.nodeId]">
                        {{ workflowImageDimensions[inputImage.nodeId].width }} ×
                        {{ workflowImageDimensions[inputImage.nodeId].height }}
                      </span>
                      <span
                        v-if="autoAspectEnabled && inputImage.label === '首帧'"
                        class="container_task_image_footer_aspect"
                      >
                        输出将自动跟随首帧比例（按 32 像素对齐）
                      </span>
                    </figcaption>
                    <div v-if="!autoAspectEnabled" class="container_task_image_body_upload">
                      <FrameDropZone v-model="inputImageFiles[inputImage.nodeId]" :label="inputImage.label" />
                    </div>
                  </figure>
                </div>
                <div v-if="autoAspectEnabled" class="container_task_images_upload_root">
                  <p class="container_task_images_upload_hint">
                    选择新图片后，可先上传查看预览；直接提交任务也会先上传图片。
                  </p>
                  <FrameDropZone v-model="firstFrameFile" label="首帧" />
                  <FrameDropZone
                    v-if="selectedWorkflow?.inputImages.some(image => image.label === '尾帧')"
                    v-model="lastFrameFile"
                    label="尾帧"
                  />
                  <p v-if="frameUploadError" class="container_task_create_form_error" role="alert">
                    {{ frameUploadError }}
                  </p>
                  <button
                    type="button"
                    class="container_task_images_upload_button"
                    :disabled="uploadingFrames || submitting || (!firstFrameFile && !lastFrameFile)"
                    @click="onUploadFrames"
                  >
                    {{ uploadingFrames ? '上传中…' : '上传并更新图片' }}
                  </button>
                </div>
                <div v-else class="container_task_images_upload_root">
                  <p class="container_task_images_upload_hint">
                    选择新图片后，可先上传查看预览；直接提交任务也会先上传图片。
                  </p>
                  <p v-if="frameUploadError" class="container_task_create_form_error" role="alert">
                    {{ frameUploadError }}
                  </p>
                  <button
                    type="button"
                    class="container_task_images_upload_button"
                    :disabled="uploadingFrames || submitting || !Object.values(inputImageFiles).some(Boolean)"
                    @click="onUploadInputImages"
                  >
                    {{ uploadingFrames ? '上传中…' : '上传并更新图片' }}
                  </button>
                </div>
              </section>
            </section>

            <!-- 02: 动态工作流参数 (按需展现) -->
            <section
              v-if="selectedWorkflow?.editableFields.length"
              class="container_task_create_card_section"
            >
              <header class="container_task_create_header_section">
                <span class="container_task_create_badge_step">02</span>
                <div>
                  <h2 class="container_task_create_header_sectitle">动态工作流参数</h2>
                  <p class="container_task_create_header_secdesc">
                    按需调整 Prompts、种子与执行参数；变动会自动持久化至个人草稿
                  </p>
                </div>
              </header>

              <div class="container_task_create_form_draftbar">
                <div class="container_task_create_cell_draftmeta">
                  <span
                    class="container_task_create_header_draftdot"
                    :data-status="workflowDraftStatus"
                  />
                  <span
                    class="container_task_create_form_drafthint"
                    :data-state="workflowDraftStatus === 'error' ? 'error' : undefined"
                  >
                    <template v-if="workflowDraftStatus === 'loading'">
                      正在从数据库读取当前用户的草稿…
                    </template>
                    <template v-else-if="workflowDraftStatus === 'saving'">
                      正在保存到当前用户的数据库草稿…
                    </template>
                    <template v-else-if="workflowDraftStatus === 'error'">
                      {{ workflowDraftErrorMessage }}；当前页面仍保留内容，请稍后重试。
                    </template>
                    <template v-else-if="workflowDraftRestored">
                      已恢复当前用户的数据库草稿；后续修改会自动保存。
                    </template>
                    <template v-else-if="workflowDraftStatus === 'saved'">
                      修改内容已保存到当前用户的数据库草稿。
                    </template>
                    <template v-else>
                      修改内容会按当前用户和工作流自动保存到数据库。
                    </template>
                  </span>
                </div>
                <button
                  class="container_task_create_form_draftreset"
                  type="button"
                  :disabled="workflowDraftStatus === 'loading' || workflowDraftResetting"
                  @click="resetWorkflowFields"
                >
                  {{ workflowDraftResetting ? '恢复中…' : '恢复默认值' }}
                </button>
              </div>

              <div class="container_task_create_form_parameters">
                <label
                  v-for="field in selectedWorkflow.editableFields"
                  :key="field.key"
                  class="container_task_create_form_parameter"
                >
                  <span>{{ field.label }}</span>
                  <textarea
                    v-if="field.control === 'textarea'"
                    v-model="workflowFieldValues[field.key]"
                    :rows="field.rows ?? 6"
                    :required="field.required"
                    :disabled="workflowDraftStatus === 'loading' || workflowDraftResetting"
                  />
                  <select
                    v-else-if="field.control === 'select'"
                    v-model="workflowFieldValues[field.key]"
                    :required="field.required"
                    :disabled="workflowDraftStatus === 'loading' || workflowDraftResetting"
                  >
                    <option
                      v-for="option in field.options ?? []"
                      :key="option.value"
                      :value="option.value"
                    >
                      {{ option.label }}
                    </option>
                  </select>
                  <input
                    v-else
                    v-model="workflowFieldValues[field.key]"
                    type="text"
                    :required="field.required"
                    :disabled="workflowDraftStatus === 'loading' || workflowDraftResetting"
                  >
                  <span v-if="field.help" class="container_task_create_form_parameterhelp">
                    {{ field.help }}
                  </span>
                </label>
              </div>
            </section>

            <!-- 03: 算力规格与执行环境 -->
            <section class="container_task_create_card_section">
              <header class="container_task_create_header_section">
                <span class="container_task_create_badge_step">{{ selectedWorkflow?.editableFields.length ? '03' : '02' }}</span>
                <div>
                  <h2 class="container_task_create_header_sectitle">算力规格与执行环境</h2>
                  <p class="container_task_create_header_secdesc">
                    指定分配的 Colab 会话、GPU/TPU 规格与生命周期策略
                  </p>
                </div>
              </header>

              <label class="container_task_create_form_session">
                <span class="container_task_create_cell_fieldlabel">会话名称</span>
                <input v-model="sessionName" type="text" required placeholder="如 comfyui-h3">
              </label>

              <div class="container_task_create_card_optionitem" :data-checked="reuse ? 'true' : 'false'">
                <label class="container_task_create_form_reuse">
                  <input v-model="reuse" type="checkbox">
                  <div class="container_task_create_cell_optionmeta">
                    <span class="container_task_create_cell_optiontitle">复用会话 (Reuse Session)</span>
                    <span class="container_task_create_cell_optionsub">
                      直接连接并使用现有同名 VM，跳过新节点初始化与环境准备耗时
                    </span>
                  </div>
                </label>
              </div>

              <p v-if="reuse && reuseTarget" class="container_task_create_form_sessioninfo">
                目标会话状态：{{ reuseStatusText(reuseTarget) }}
                <template v-if="reuseTarget.variant">（{{ reuseTarget.variant }}）</template>
              </p>
              <p v-if="reuse && reuseProblem" class="container_task_create_form_error" data-state="error">
                {{ reuseProblem }}
              </p>

              <fieldset v-if="!reuse" class="container_task_create_toolbar_gpu">
                <legend class="container_task_create_form_legend">加速器与算力规格</legend>
                <label class="container_task_create_form_spec">
                  <span class="container_task_create_cell_speclabel">计算后端</span>
                  <select v-model="backend" name="compute-backend">
                    <option v-for="item in COMPUTE_BACKENDS" :key="item" :value="item">{{ BACKEND_LABELS[item] }}</option>
                  </select>
                </label>
                <div class="container_task_create_panel_pillgroup">
                  <label
                    v-for="kind in (['gpu', 'tpu'] as const)"
                    :key="kind"
                    class="container_task_create_form_kind"
                    :aria-disabled="!acceleratorSupported(kind)"
                    :data-disabled="!acceleratorSupported(kind) ? 'true' : 'false'"
                    :data-checked="acceleratorKind === kind ? 'true' : 'false'"
                    :title="acceleratorDisabledTitle(kind)"
                  >
                    <input
                      v-model="acceleratorKind"
                      type="radio"
                      name="accelerator-kind"
                      :value="kind"
                      :disabled="!acceleratorSupported(kind)"
                    >
                    <span>{{ kind.toUpperCase() }}</span>
                  </label>
                </div>
                <label class="container_task_create_form_spec">
                  <span class="container_task_create_cell_speclabel">规格</span>
                  <select v-model="acceleratorSpec">
                    <option v-for="spec in specOptions" :key="spec" :value="spec">{{ spec }}</option>
                  </select>
                </label>
              </fieldset>

              <div class="container_task_create_card_options">
                <label
                  class="container_task_create_card_optionitem"
                  :data-checked="mountDrive ? 'true' : 'false'"
                >
                  <input v-model="mountDrive" type="checkbox">
                  <div class="container_task_create_cell_optionmeta">
                    <span class="container_task_create_cell_optiontitle">挂载 Google Drive</span>
                    <span class="container_task_create_cell_optionsub">持久化模型权重与输出缓存，显著加速后续执行</span>
                  </div>
                </label>

                <label
                  class="container_task_create_card_optionitem"
                  :data-checked="keep ? 'true' : 'false'"
                >
                  <input v-model="keep" type="checkbox">
                  <div class="container_task_create_cell_optionmeta">
                    <span class="container_task_create_cell_optiontitle">保留 VM 会话</span>
                    <span class="container_task_create_cell_optionsub">任务完成后不销毁会话，保持存活（按需计费）</span>
                  </div>
                </label>

                <label
                  class="container_task_create_card_optionitem"
                  :data-checked="dryRun ? 'true' : 'false'"
                >
                  <input v-model="dryRun" type="checkbox">
                  <div class="container_task_create_cell_optionmeta">
                    <span class="container_task_create_cell_optiontitle">演练模式 (dry_run)</span>
                    <span class="container_task_create_cell_optionsub">验证调度流程与参数合法性，不分配计算资源</span>
                  </div>
                </label>
              </div>

              <label class="container_task_create_form_timeout">
                <span class="container_task_create_cell_fieldlabel">超时保护（秒）</span>
                <input v-model.number="timeoutSeconds" type="number" min="1" step="1" required>
                <span class="container_task_create_form_parameterhelp">
                  默认 86400 秒（24小时），超时将由 Runner 强行终止并置为超时状态
                </span>
              </label>
            </section>
          </div>

          <!-- 右侧：提交决策摘要侧边栏 (Sticky) -->
          <aside class="container_task_create_panel_side">
            <div class="container_task_create_card_summary">
              <header class="container_task_create_header_summary">
                <h2 class="container_task_create_header_subtitle">提交决策摘要</h2>
                <span class="container_task_create_badge_accent">Pre-flight</span>
              </header>

              <div class="container_task_create_cell_summarylist">
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">所选工作流</span>
                  <span class="container_task_create_cell_summaryval" :title="selectedWorkflow?.name || '未选择'">
                    {{ selectedWorkflow ? selectedWorkflow.name : '未选择' }}
                  </span>
                </div>
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">算力架构</span>
                  <span class="container_task_create_cell_summaryval">
                    {{ reuse ? '复用现有 VM' : `${BACKEND_LABELS[backend]} · ${acceleratorKind.toUpperCase()} · ${acceleratorSpec}` }}
                  </span>
                </div>
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">目标会话</span>
                  <span class="container_task_create_cell_summaryval" :title="sessionName">
                    {{ sessionName || '未指定' }}
                  </span>
                </div>
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">Drive 缓存</span>
                  <span class="container_task_create_cell_summaryval">
                    {{ mountDrive ? '已挂载' : '未挂载' }}
                  </span>
                </div>
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">VM 生命周期</span>
                  <span class="container_task_create_cell_summaryval">
                    {{ keep ? '任务后保留' : '完成后释放' }}
                  </span>
                </div>
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">运行模式</span>
                  <span class="container_task_create_cell_summaryval" :data-dryrun="dryRun ? 'true' : 'false'">
                    {{ dryRun ? '演练模式 (调试)' : '真实生产' }}
                  </span>
                </div>
                <div class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">超时限制</span>
                  <span class="container_task_create_cell_summaryval">
                    {{ formattedTimeout }}
                  </span>
                </div>
                <div v-if="selectedWorkflow?.editableFields.length" class="container_task_create_cell_summaryitem">
                  <span class="container_task_create_cell_summarylabel">参数草稿</span>
                  <span class="container_task_create_cell_summaryval" :data-status="workflowDraftStatus">
                    {{ draftStatusLabel }}
                  </span>
                </div>
              </div>

              <div v-if="errorMessage || (reuse && reuseProblem)" class="container_task_create_card_alert" role="alert">
                <span class="container_task_create_cell_alerticon">⚠️</span>
                <span class="container_task_create_cell_alerttext">{{ errorMessage || reuseProblem }}</span>
              </div>

              <button
                class="container_task_create_footer_submit"
                type="submit"
                :disabled="submitting || uploadingFrames || workflowDraftStatus === 'loading' || workflowDraftResetting"
              >
                <span v-if="submitting">🚀 正在提交并调度…</span>
                <span v-else-if="uploadingFrames">📤 正在上传图片…</span>
                <span v-else>立即提交任务</span>
              </button>

              <p class="container_task_create_cell_submithint">
                提交后将由 Runner 调度并自动跳转至实时监控看板
              </p>
            </div>
          </aside>
        </div>
      </form>
    </template>
  </div>
</template>

<style>
.container_task_create_form_root {
  max-width: var(--w-page);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_task_create_header_root {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-4);
}

.container_task_create_header_title {
  margin: 0;
  font-size: var(--fs-2xl);
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--c-text);
}

.container_task_create_header_desc {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

.container_task_create_tabs_root {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-6);
  border-bottom: 1px solid var(--c-border);
}

.container_task_create_tabs_link {
  padding: var(--sp-2) var(--sp-4);
  font-weight: 500;
  color: var(--c-text-sub);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  transition: color var(--t-fast), border-color var(--t-fast);
}

.container_task_create_tabs_link:hover {
  color: var(--c-text);
}

.container_task_create_tabs_link[data-active='true'] {
  color: var(--c-brand);
  border-color: var(--c-brand);
  text-shadow: var(--c-brand-glow-subtle);
}

.container_task_create_form_denied {
  max-width: var(--w-form);
  margin: var(--sp-6) auto;
  padding: var(--sp-4);
  color: var(--c-danger);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_task_create_form_hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-6);
  padding: var(--sp-3) var(--sp-4);
  color: var(--c-warn-text);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-md);
}

.container_task_create_toolbar_sync {
  padding: var(--sp-1) var(--sp-3);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--c-text);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_task_create_form_body {
  margin: 0;
  padding: 0;
  background: transparent;
  border: none;
}

.container_task_create_panel_layout {
  display: flex;
  flex-direction: column;
  gap: var(--sp-6);
}

.container_task_create_panel_main {
  display: flex;
  flex-direction: column;
  gap: var(--sp-6);
  min-width: 0;
}

.container_task_create_card_section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-6);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
  transition: border-color var(--t-base), box-shadow var(--t-base);
}

.container_task_create_card_section:focus-within {
  border-color: var(--c-border-strong);
}

.container_task_create_header_section {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--c-divider);
}

.container_task_create_badge_step {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  font-family: var(--ff-mono);
  font-size: var(--fs-xs);
  font-weight: 700;
  color: var(--c-brand);
  background: var(--c-brand-bg);
  border: 1px solid var(--c-brand-border);
  border-radius: var(--r-sm);
}

.container_task_create_header_sectitle {
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: 600;
  line-height: 1.3;
  color: var(--c-text);
}

.container_task_create_header_secdesc {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-xs);
  color: var(--c-text-sub);
}

.container_task_create_cell_fieldlabel {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--c-text);
}

.container_task_create_form_workflow {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.container_task_create_form_workflow select {
  width: 100%;
  padding: 10px var(--sp-3);
  font-size: var(--fs-md);
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border-strong);
  border-radius: var(--r-sm);
  transition: border-color var(--t-fast), box-shadow var(--t-fast);
}

.container_task_create_form_modelsize {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-sm);
  line-height: 1.5;
  color: var(--c-warn-text);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-sm);
}

.container_task_create_cell_modelsizetag {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  font-family: var(--ff-mono);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--c-warn-text);
  white-space: nowrap;
  background: var(--c-surface);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-sm);
}

.container_task_create_form_images {
  padding: var(--sp-4);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_task_images_header_title {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text);
}

.container_task_images_body_grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}

.container_task_image_body_card {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_task_image_body_preview,
.container_task_image_body_empty {
  display: flex;
  width: 100%;
  height: 180px;
  align-items: center;
  justify-content: center;
  color: var(--c-text-weak);
  background: var(--c-surface-hover);
  object-fit: contain;
}

.container_task_image_footer_caption {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2) 10px;
}

.container_task_image_footer_aspect {
  color: var(--c-brand);
  font-size: var(--fs-xs);
}

.container_task_image_footer_caption span {
  overflow: hidden;
  color: var(--c-text-weak);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container_task_image_body_upload {
  padding: var(--sp-2);
}

.container_task_images_upload_root {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-top: var(--sp-4);
}

.container_task_images_upload_hint {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

.container_task_images_upload_button {
  align-self: flex-start;
  padding: var(--sp-2) var(--sp-4);
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: 0;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast);
}

.container_task_images_upload_button:hover:not(:disabled) {
  filter: brightness(1.1);
}

.container_task_create_form_draftbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_task_create_cell_draftmeta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}

.container_task_create_header_draftdot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  background: var(--c-text-weak);
  border-radius: 50%;
  transition: background-color var(--t-fast);
}

.container_task_create_header_draftdot[data-status='loading'],
.container_task_create_header_draftdot[data-status='saving'] {
  background: var(--c-brand);
  animation: pulse-draft-dot 1.2s infinite ease-in-out;
}

.container_task_create_header_draftdot[data-status='saved'] {
  background: var(--c-success);
  box-shadow: 0 0 6px var(--c-success);
}

.container_task_create_header_draftdot[data-status='error'] {
  background: var(--c-danger);
  box-shadow: 0 0 6px var(--c-danger);
}

@keyframes pulse-draft-dot {
  0%, 100% {
    transform: scale(0.9);
    opacity: 0.5;
  }

  50% {
    transform: scale(1.3);
    opacity: 1;
  }
}

.container_task_create_form_drafthint {
  overflow: hidden;
  font-size: var(--fs-xs);
  color: var(--c-text-sub);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container_task_create_form_drafthint[data-state='error'] {
  color: var(--c-danger);
}

.container_task_create_form_draftreset {
  flex: none;
  padding: var(--sp-1) 10px;
  font-size: var(--fs-xs);
  color: var(--c-brand);
  background: transparent;
  border: 1px solid var(--c-brand-border);
  border-radius: var(--r-sm);
  transition: all var(--t-fast);
}

.container_task_create_form_draftreset:hover:not(:disabled) {
  background: var(--c-brand-bg);
  border-color: var(--c-brand);
}

.container_task_create_form_parameters {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  margin: 0;
  padding: 0;
  border: none;
}

.container_task_create_form_parameter {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--c-text);
}

.container_task_create_form_parameter input,
.container_task_create_form_parameter select,
.container_task_create_form_parameter textarea {
  width: 100%;
  padding: 10px var(--sp-3);
  font-family: var(--ff-mono);
  font-size: var(--fs-sm);
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border-strong);
  border-radius: var(--r-sm);
}

.container_task_create_form_parameter textarea {
  min-height: 120px;
  line-height: 1.5;
  resize: vertical;
}

.container_task_create_form_parameterhelp {
  font-size: var(--fs-xs);
  font-weight: normal;
  color: var(--c-text-weak);
}

.container_task_create_form_session {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.container_task_create_form_session input {
  width: 100%;
  padding: 10px var(--sp-3);
  font-family: var(--ff-mono);
  font-size: var(--fs-md);
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border-strong);
  border-radius: var(--r-sm);
}

.container_task_create_card_options {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--sp-3);
}

.container_task_create_card_optionitem {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  cursor: pointer;
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
  transition: all var(--t-fast);
}

.container_task_create_card_optionitem:hover {
  background: var(--c-surface-hover);
  border-color: var(--c-border-strong);
}

.container_task_create_card_optionitem[data-checked='true'] {
  background: var(--c-brand-bg);
  border-color: var(--c-brand);
  box-shadow: var(--sh-glow-brand);
}

.container_task_create_card_optionitem input[type='checkbox'] {
  flex-shrink: 0;
  margin-top: 3px;
}

.container_task_create_cell_optionmeta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.container_task_create_cell_optiontitle {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--c-text);
}

.container_task_create_cell_optionsub {
  font-size: var(--fs-xs);
  line-height: 1.4;
  color: var(--c-text-sub);
}

.container_task_create_form_reuse {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  width: 100%;
  cursor: pointer;
}

.container_task_create_form_sessioninfo {
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--c-text-sub);
  background: var(--c-surface-sub);
  border-left: 3px solid var(--c-brand);
  border-radius: var(--r-sm);
}

.container_task_create_toolbar_gpu {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  margin: 0;
  padding: var(--sp-4);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_task_create_form_legend {
  padding: 0 var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.5px;
  color: var(--c-text-sub);
  text-transform: uppercase;
}

.container_task_create_panel_pillgroup {
  display: flex;
  gap: var(--sp-2);
}

.container_task_create_form_kind {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 8px 16px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--c-text-sub);
  cursor: pointer;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
  transition: all var(--t-fast);
}

.container_task_create_form_kind:hover:not([data-disabled='true']) {
  color: var(--c-text);
  border-color: var(--c-border-strong);
}

.container_task_create_form_kind[data-checked='true'] {
  color: var(--c-brand);
  background: var(--c-brand-bg);
  border-color: var(--c-brand);
  box-shadow: var(--sh-glow-brand);
}

.container_task_create_form_kind[data-disabled='true'] {
  color: var(--c-text-weak);
  cursor: not-allowed;
  opacity: 0.5;
}

.container_task_create_form_kind input[type='radio'] {
  display: none;
}

.container_task_create_form_spec {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.container_task_create_cell_speclabel {
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
  white-space: nowrap;
}

.container_task_create_form_spec select {
  padding: 6px var(--sp-3);
  font-family: var(--ff-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--c-brand);
  background: var(--c-surface);
  border: 1px solid var(--c-border-strong);
  border-radius: var(--r-sm);
}

.container_task_create_form_timeout {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.container_task_create_form_timeout input {
  width: 100%;
  padding: 10px var(--sp-3);
  font-family: var(--ff-mono);
  font-size: var(--fs-md);
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border-strong);
  border-radius: var(--r-sm);
}

.container_task_create_form_error {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--c-danger);
}

.container_task_create_panel_side {
  width: 100%;
}

.container_task_create_card_summary {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-6);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-top: 3px solid var(--c-brand);
  border-radius: var(--r-md);
  box-shadow: var(--sh-2);
}

.container_task_create_header_summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--c-divider);
}

.container_task_create_header_subtitle {
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--c-text);
}

.container_task_create_badge_accent {
  display: inline-block;
  padding: 2px 8px;
  font-family: var(--ff-mono);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--c-brand);
  background: var(--c-brand-bg);
  border: 1px solid var(--c-brand-border);
  border-radius: var(--r-sm);
}

.container_task_create_cell_summarylist {
  display: flex;
  flex-direction: column;
}

.container_task_create_cell_summaryitem {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: 10px 0;
  border-bottom: 1px solid var(--c-divider);
}

.container_task_create_cell_summarylabel {
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

.container_task_create_cell_summaryval {
  max-width: 180px;
  overflow: hidden;
  font-family: var(--ff-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--c-text);
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container_task_create_cell_summaryval[data-dryrun='true'] {
  color: var(--c-warn);
}

.container_task_create_cell_summaryval[data-status='error'] {
  color: var(--c-danger);
}

.container_task_create_cell_summaryval[data-status='saved'] {
  color: var(--c-success);
}

.container_task_create_card_alert {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--c-danger);
  background: var(--c-danger-bg);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
}

.container_task_create_cell_alerticon {
  flex-shrink: 0;
}

.container_task_create_cell_alerttext {
  line-height: 1.4;
  overflow-wrap: break-word;
}

.container_task_create_footer_submit {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  width: 100%;
  padding: 14px 0;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text-invert);
  cursor: pointer;
  background: var(--c-brand);
  border: none;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast), transform var(--t-fast);
}

.container_task_create_footer_submit:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.container_task_create_footer_submit:hover:not(:disabled) {
  filter: brightness(1.1);
  transform: translateY(-1px);
}

.container_task_create_footer_submit:active:not(:disabled) {
  transform: scale(0.99);
}

.container_task_create_cell_submithint {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.4;
  color: var(--c-text-weak);
  text-align: center;
}

/* 桌面端双列布局：左主配置区 + 右侧常驻粘性决策摘要（doc/09 §2 & §5） */
@media (width >= 1024px) {
  .container_task_create_panel_layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 360px;
    gap: var(--sp-6);
    align-items: start;
  }

  .container_task_create_panel_side {
    position: sticky;
    top: var(--sp-4);
  }
}

/* 窄屏响应式处理 */
@media (width <= 768px) {
  .container_task_create_header_root {
    flex-direction: column;
    align-items: flex-start;
  }

  .container_task_create_toolbar_gpu,
  .container_task_create_form_draftbar,
  .container_task_create_form_hint {
    flex-wrap: wrap;
    gap: var(--sp-2);
  }

  .container_task_create_card_section,
  .container_task_create_card_summary {
    padding: var(--sp-4);
  }

  .container_task_create_form_root {
    padding: var(--sp-4) var(--sp-3);
  }
}
</style>
