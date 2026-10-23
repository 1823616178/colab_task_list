<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { useUiStore } from '~/stores/ui'
import { useWorkflowStore } from '~/stores/workflow'

// /workflows/:slug/frames 首尾帧设置（operator+，doc/04 §2，替代手工 set_frames.py）。
// multipart POST /api/workflows/:slug/frames → Nitro 落临时文件 → 转发 Runner。
// ⚠️ 尾帧是否可用取决于目标工作流 MiniMaxH3ImageToVideo 是否接了 last_frame。
// 仓库根 api.json（minimax_h3）已接节点 122；未接线的工作流 Runner 会明确拒绝，
// 后端错误原样展示，不做预判（doc/02 §2.2）。
const route = useRoute()
const authStore = useAuthStore()
const workflowStore = useWorkflowStore()
const uiStore = useUiStore()

const slug = computed(() => String(route.params.slug))
const workflow = computed(() =>
  workflowStore.workflows.find(item => item.slug === slug.value) ?? null)

// 工作流名要等 /api/workflows 回来才有，先用 slug 占位
useHead({ title: () => `设首尾帧：${workflow.value?.name ?? slug.value}` })

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

const firstFile = ref<File | null>(null)
const lastFile = ref<File | null>(null)
const submitting = ref(false)
const errorMessage = ref('')
const resultOutput = ref('')

interface FramesResult {
  ok: boolean
  slug: string
  api_json: string
  image_dir: string
  output: string
}

async function onSubmit() {
  errorMessage.value = ''
  resultOutput.value = ''
  if (!firstFile.value && !lastFile.value) {
    errorMessage.value = '至少需要提供首帧或尾帧之一'
    return
  }
  submitting.value = true
  try {
    const formData = new FormData()
    if (firstFile.value) formData.append('first', firstFile.value)
    if (lastFile.value) formData.append('last', lastFile.value)
    const { $apiFetch } = useNuxtApp()
    const result = await $apiFetch<FramesResult>(
      `/api/workflows/${encodeURIComponent(slug.value)}/frames`,
      { method: 'POST', body: formData },
    )
    resultOutput.value = result.output || '已更新'
    uiStore.info(`工作流 ${slug.value} 首尾帧已更新`)
  } catch (err) {
    // 后端错误（如 last_frame 未接线被 Runner 拒绝）原样展示
    errorMessage.value = extractApiError(err, '设置首尾帧失败')
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  if (workflowStore.workflows.length === 0) {
    try {
      await workflowStore.fetchWorkflows()
    } catch (err) {
      uiStore.error(extractApiError(err, '加载工作流信息失败'))
    }
  }
})
</script>

<template>
  <div class="container_workflow_frames_form_root">
    <h1 class="container_workflow_frames_header_title">
      设首尾帧：{{ workflow?.name ?? slug }}
    </h1>

    <p v-if="!isOperator" class="container_workflow_frames_form_denied" role="alert">
      当前角色为只读，无权设置首尾帧。
    </p>

    <template v-else>
      <p class="container_workflow_frames_form_hint">
        上传后将更新该工作流的 api.json（{{ workflow?.apiJsonPath ?? '…' }}）中
        MiniMaxH3ImageToVideo 节点引用的 LoadImage 图片，并把图片复制到 image/ 目录。
        已受理的任务不受影响（提交时快照，doc/02 §2.3.1）。
      </p>

      <form class="container_workflow_frames_form_body" @submit.prevent="onSubmit">
        <FrameDropZone v-model="firstFile" label="首帧" />
        <FrameDropZone
          v-if="workflow?.inputImages.some(image => image.label === '尾帧')"
          v-model="lastFile"
          label="尾帧"
          hint="用于控制视频结尾画面；首次使用前请设置工作流所需图片"
        />

        <p v-if="errorMessage" class="container_workflow_frames_form_error" data-state="error">
          {{ errorMessage }}
        </p>
        <p v-if="resultOutput" class="container_workflow_frames_form_result" data-state="ok">
          {{ resultOutput }}
        </p>

        <button
          class="container_workflow_frames_footer_submit"
          type="submit"
          :disabled="submitting || (!firstFile && !lastFile)"
        >
          {{ submitting ? '上传中…' : '上传并更新 api.json' }}
        </button>
      </form>
    </template>
  </div>
</template>

<style>
.container_workflow_frames_form_root {
  max-width: var(--w-form);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_workflow_frames_header_title {
  margin: 0 0 var(--sp-4);
  font-size: var(--fs-xl);
}

.container_workflow_frames_form_denied {
  padding: var(--sp-4);
  color: var(--c-danger);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_workflow_frames_form_hint {
  padding: var(--sp-3) var(--sp-4);
  color: var(--c-text-sub);
  background: var(--c-surface-hover);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_workflow_frames_form_body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-top: 2px solid var(--c-brand);
  border-radius: var(--r-md);
  box-shadow: var(--sh-2);
}

.container_workflow_frames_form_error {
  margin: 0;
  color: var(--c-danger);
}

.container_workflow_frames_form_result {
  margin: 0;
  color: var(--c-success);
  white-space: pre-wrap;
}

.container_workflow_frames_footer_submit {
  padding: 12px 0;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast), transform var(--t-fast);
}

.container_workflow_frames_footer_submit:hover:not(:disabled) {
  filter: brightness(1.1);
  transform: translateY(-1px);
}

</style>
