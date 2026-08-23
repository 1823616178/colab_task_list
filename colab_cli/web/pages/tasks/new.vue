<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { useSessionStore } from '~/stores/session'
import type { SessionItem } from '~/stores/session'
import { useTaskStore } from '~/stores/task'
import type { SubmitTaskPayload } from '~/stores/task'
import { useUiStore } from '~/stores/ui'
import { useWorkflowStore } from '~/stores/workflow'
import {
  parseWorkflowDraftRecord,
  serializeWorkflowDraft,
  workflowDraftStorageKey,
} from '~/utils/workflowDraft'

// /tasks/new 提交任务（operator+，doc/04 §2.1）：
// 字段对齐 Runner POST /tasks 请求体（doc/02 §2.3），并把
// validate_workflow_accelerator()（run.py:1105）的规则搬到前端做前置校验。
useHead({ title: '提交任务' })

const authStore = useAuthStore()
const workflowStore = useWorkflowStore()
const sessionStore = useSessionStore()
const taskStore = useTaskStore()
const uiStore = useUiStore()
const apiFetch = useNuxtApp().$apiFetch

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

const GPU_SPECS = ['T4', 'L4', 'G4', 'H100', 'A100']
const TPU_SPECS = ['v5e1', 'v6e1']
const SESSION_NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/

// ── 表单字段（与 doc/02 §2.3 请求体一一对应）─────────────────────
const workflowConfig = ref('')
const sessionName = ref('comfyui-h3')
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

watch([selectedWorkflow, () => authStore.user?.id], async ([workflow, userId]) => {
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

watch(selectedWorkflow, async (workflow) => {
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
})

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

const specOptions = computed(() => (acceleratorKind.value === 'gpu' ? GPU_SPECS : TPU_SPECS))

watch(acceleratorKind, kind => {
  acceleratorSpec.value = kind === 'gpu' ? GPU_SPECS[0]! : TPU_SPECS[0]!
})

/** doc/04 §2.1：supportedAccelerators 之外的加速器置灰，title 显示 acceleratorNote */
function acceleratorSupported(kind: 'gpu' | 'tpu'): boolean {
  const wf = selectedWorkflow.value
  if (!wf || wf.supportedAccelerators.length === 0) return true
  return wf.supportedAccelerators.includes(kind)
}

function acceleratorDisabledTitle(kind: 'gpu' | 'tpu'): string {
  if (acceleratorSupported(kind)) return ''
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
    const payload: SubmitTaskPayload = {
      workflow_config: workflowConfig.value,
      api_json: null,
      session: sessionName.value.trim(),
      accelerator: { kind: acceleratorKind.value, spec: acceleratorSpec.value },
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

onMounted(async () => {
  try {
    await workflowStore.fetchWorkflows()
  } catch (err) {
    uiStore.error(extractApiError(err, '加载工作流列表失败'))
  }
})
</script>

<template>
  <div class="container_task_create_form_root">
    <h1 class="container_task_create_header_title">提交任务</h1>

    <p v-if="!isOperator" class="container_task_create_form_denied" role="alert">
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
        <label class="container_task_create_form_workflow">
          工作流
          <select v-model="workflowConfig" required>
            <option value="" disabled>请选择工作流</option>
            <option v-for="wf in workflowStore.workflows" :key="wf.slug" :value="wf.configPath">
              {{ wf.name }}（{{ wf.slug }}）
            </option>
          </select>
        </label>

        <p v-if="totalModelGiB !== null" class="container_task_create_form_modelsize">
          模型总体积：{{ totalModelGiB.toFixed(1) }} GiB；
          首次运行需下载 ~{{ Math.ceil(totalModelGiB) }} GiB（后续命中 Drive 缓存会明显加快）。
        </p>

        <section
          v-if="selectedWorkflow?.inputImages?.length"
          class="container_task_create_form_images"
        >
          <h2 class="container_task_images_header_title">当前输入图片</h2>
          <div class="container_task_images_body_grid">
            <figure
              v-for="inputImage in selectedWorkflow.inputImages"
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
            </figure>
          </div>
        </section>

        <fieldset
          v-if="selectedWorkflow?.editableFields.length"
          class="container_task_create_form_parameters"
        >
          <legend class="container_task_create_form_legend">工作流参数</legend>
          <div class="container_task_create_form_draftbar">
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
            <button
              class="container_task_create_form_draftreset"
              type="button"
              :disabled="workflowDraftStatus === 'loading' || workflowDraftResetting"
              @click="resetWorkflowFields"
            >
              恢复默认值
            </button>
          </div>
          <label
            v-for="field in selectedWorkflow.editableFields"
            :key="field.key"
            class="container_task_create_form_parameter"
          >
            {{ field.label }}
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
        </fieldset>

        <label class="container_task_create_form_session">
          会话名
          <input v-model="sessionName" type="text" required>
        </label>

        <label class="container_task_create_form_reuse">
          <input v-model="reuse" type="checkbox">
          复用会话（使用现有 VM，跳过新建与环境准备）
        </label>
        <p v-if="reuse && reuseTarget" class="container_task_create_form_sessioninfo">
          目标会话状态：{{ reuseStatusText(reuseTarget) }}
          <template v-if="reuseTarget.variant">（{{ reuseTarget.variant }}）</template>
        </p>
        <p v-if="reuse && reuseProblem" class="container_task_create_form_error" data-state="error">
          {{ reuseProblem }}
        </p>

        <fieldset v-if="!reuse" class="container_task_create_toolbar_gpu">
          <legend class="container_task_create_form_legend">加速器</legend>
          <label
            v-for="kind in (['gpu', 'tpu'] as const)"
            :key="kind"
            class="container_task_create_form_kind"
            :aria-disabled="!acceleratorSupported(kind)"
            :data-disabled="!acceleratorSupported(kind) ? 'true' : 'false'"
            :title="acceleratorDisabledTitle(kind)"
          >
            <input
              v-model="acceleratorKind"
              type="radio"
              name="accelerator-kind"
              :value="kind"
              :disabled="!acceleratorSupported(kind)"
            >
            {{ kind.toUpperCase() }}
          </label>
          <label class="container_task_create_form_spec">
            规格
            <select v-model="acceleratorSpec">
              <option v-for="spec in specOptions" :key="spec" :value="spec">{{ spec }}</option>
            </select>
          </label>
        </fieldset>

        <label class="container_task_create_form_mountdrive">
          <input v-model="mountDrive" type="checkbox">
          挂载 Google Drive（模型缓存）
        </label>
        <label class="container_task_create_form_keep">
          <input v-model="keep" type="checkbox">
          保留 VM（任务结束后不销毁会话）
        </label>

        <label class="container_task_create_form_timeout">
          超时时间（秒）
          <input v-model.number="timeoutSeconds" type="number" min="1" step="1" required>
        </label>

        <label class="container_task_create_form_dryrun">
          <input v-model="dryRun" type="checkbox">
          演练模式（dry_run，不消耗计算单元，仅调试）
        </label>

        <p v-if="errorMessage" class="container_task_create_form_error" data-state="error">
          {{ errorMessage }}
        </p>

        <button
          class="container_task_create_footer_submit"
          type="submit"
          :disabled="submitting || workflowDraftStatus === 'loading' || workflowDraftResetting"
        >
          {{ submitting ? '提交中…' : '提交任务' }}
        </button>
      </form>
    </template>
  </div>
</template>

<style>
.container_task_create_form_root {
  max-width: var(--w-form);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_task_create_header_title {
  margin: 0 0 var(--sp-4);
  font-size: var(--fs-xl);
}

.container_task_create_form_denied {
  padding: var(--sp-4);
  color: var(--c-danger);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_create_form_hint {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-md);
}

.container_task_create_toolbar_sync {
  padding: var(--sp-1) var(--sp-3);
}

.container_task_create_form_body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_create_form_workflow,
.container_task_create_form_session,
.container_task_create_form_parameter,
.container_task_create_form_timeout,
.container_task_create_form_spec {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.container_task_create_form_workflow select,
.container_task_create_form_session input,
.container_task_create_form_parameter input,
.container_task_create_form_parameter select,
.container_task_create_form_parameter textarea,
.container_task_create_form_timeout input,
.container_task_create_form_spec select {
  padding: var(--sp-2) 10px;
  border: 1px solid var(--c-border-strong);
  border-radius: var(--r-sm);
}

.container_task_create_form_parameters {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_task_create_form_parameter textarea {
  min-height: 120px;
  resize: vertical;
}

.container_task_create_form_parameterhelp {
  color: var(--c-text-weak);
  font-size: var(--fs-xs);
}

.container_task_create_form_draftbar {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  justify-content: space-between;
}

.container_task_create_form_drafthint {
  color: var(--c-text-sub);
  font-size: var(--fs-xs);
}

.container_task_create_form_drafthint[data-state='error'] {
  color: var(--c-danger);
}

.container_task_create_form_draftreset {
  flex: none;
  padding: var(--sp-1) 10px;
  color: var(--c-brand);
  border: 1px solid var(--c-brand-hover);
}

.container_task_create_form_modelsize {
  margin: 0;
  color: var(--c-warn-text);
}

.container_task_create_form_images {
  padding: var(--sp-3);
  background: var(--c-surface-hover);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_task_images_header_title {
  margin: 0 0 10px;
  font-size: 15px;
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

.container_task_create_form_reuse,
.container_task_create_form_mountdrive,
.container_task_create_form_keep,
.container_task_create_form_dryrun {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
}

.container_task_create_form_sessioninfo {
  margin: 0;
  color: var(--c-text-sub);
}

.container_task_create_toolbar_gpu {
  display: flex;
  gap: var(--sp-4);
  align-items: center;
  padding: var(--sp-3);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_task_create_form_legend {
  padding: 0 var(--sp-1);
  color: var(--c-text-sub);
}

.container_task_create_form_kind {
  display: flex;
  gap: 6px;
  align-items: center;
}

.container_task_create_form_kind[data-disabled='true'] {
  color: var(--c-text-weak);
  cursor: not-allowed;
}

.container_task_create_form_error {
  margin: 0;
  color: var(--c-danger);
}

.container_task_create_footer_submit {
  padding: 10px 0;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
}

/* 窄屏：把几处横排的控件组改成换行/单列。表单本身已经是 --w-form
   单列布局，这里只处理会撑破视口的横排行。 */
@media (width <= 768px) {
  .container_task_create_toolbar_gpu,
  .container_task_create_form_draftbar,
  .container_task_create_form_hint {
    flex-wrap: wrap;
    gap: var(--sp-2);
  }

  .container_task_create_form_root {
    padding: var(--sp-4) var(--sp-3);
  }
}
</style>
