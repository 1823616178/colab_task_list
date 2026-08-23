<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { isTerminalStatus, TASK_STATUS_LABELS, useTaskStore } from '~/stores/task'
import type { TaskRow } from '~/stores/task'
import { useTaskStreamStore } from '~/stores/taskStream'
import { useUiStore } from '~/stores/ui'

// /tasks/:id 任务详情（viewer+，doc/04 §2.2）：三区数据源与刷新频率不同 ——
// 阶段时间线（task_events + SSE 里程碑）/ 资源图表（SSE metrics.sample，2s）/
// 实时日志（SSE log.line，高频，虚拟滚动）。终态按 status 切换底部动作区。
const route = useRoute()
const authStore = useAuthStore()
const taskStore = useTaskStore()
const streamStore = useTaskStreamStore()
const uiStore = useUiStore()

const taskId = route.params.id as string

useHead({ title: `任务 ${taskId}` })
const task = ref<TaskRow | null>(null)
const loadError = ref('')
const actionPending = ref(false)
const driveConfirmPending = ref(false)
const driveConfirmSent = ref(false)

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

const isTerminal = computed(() => (task.value ? isTerminalStatus(task.value.status) : false))

const connectionLabels: Record<string, string> = {
  idle: '未连接',
  connecting: '连接中…',
  open: '实时连接已建立',
  reconnecting: '连接断开，重连中…',
  closed: '连接已关闭',
}

async function reloadTask() {
  try {
    task.value = await taskStore.fetchTask(taskId)
    loadError.value = ''
  } catch (err) {
    loadError.value = extractApiError(err, '加载任务失败')
  }
}

// SSE 终态正常会在数据库收尾后到达；短暂重试兼容旧服务/网络乱序。
watch(() => streamStore.terminal, async value => {
  if (!value) return
  for (const delayMs of [0, 100, 300, 700]) {
    if (delayMs > 0) await new Promise(resolve => setTimeout(resolve, delayMs))
    await reloadTask()
    if (task.value && isTerminalStatus(task.value.status)) return
  }
})

onMounted(async () => {
  await reloadTask()
  // 终态任务不建长连：服务端回放事件后关闭；日志走 run.log（store 内部处理）
  streamStore.open(taskId, { terminal: task.value ? isTerminalStatus(task.value.status) : false })
})

onUnmounted(() => {
  streamStore.close() // doc/04 §3.2：离开必须 close
})

async function onCancel(mode: 'interrupt' | 'destroy') {
  if (mode === 'destroy') {
    // doc/02 §2.3.2：destroy 必须二次确认并提示产物丢失
    if (!window.confirm('确认销毁 VM 取消任务？\nVM 将被销毁并停止计费，远端产物全部丢失。')) return
    if (!window.confirm('再次确认：产物会丢失，无法恢复。仍要销毁吗？')) return
  } else if (!window.confirm('确认中断任务？\n内核将被重启，VM 保留、模型仍在磁盘，可稍后重投。')) {
    return
  }
  actionPending.value = true
  try {
    await taskStore.cancelTask(taskId, mode)
    uiStore.info('已发起取消')
    await reloadTask()
  } catch (err) {
    uiStore.error(extractApiError(err, '取消失败'))
  } finally {
    actionPending.value = false
  }
}

async function onRedownload() {
  actionPending.value = true
  try {
    await taskStore.redownloadTask(taskId)
    uiStore.info('已发起重试下载，任务转回运行中')
    await reloadTask()
    // 任务转回 running：重新打开事件流（此时非终态，建立长连）
    streamStore.open(taskId, { terminal: false })
  } catch (err) {
    uiStore.error(extractApiError(err, '重试下载失败'))
  } finally {
    actionPending.value = false
  }
}

async function onConfirmDrive() {
  driveConfirmPending.value = true
  try {
    await taskStore.confirmDrive(taskId)
    driveConfirmSent.value = true
    uiStore.info('已通知 Runner 继续挂载 Google Drive')
  } catch (err) {
    uiStore.error(extractApiError(err, 'Drive 授权确认失败'))
  } finally {
    driveConfirmPending.value = false
  }
}

async function onDownload(kind: 'output' | 'log') {
  actionPending.value = true
  try {
    await downloadApiFile(
      `/api/tasks/${taskId}/${kind}`,
      kind === 'output' ? `output-${taskId}.zip` : `${taskId}.log`,
    )
  } catch (err) {
    uiStore.error(err instanceof Error ? err.message : '下载失败')
  } finally {
    actionPending.value = false
  }
}
</script>

<template>
  <div class="container_task_detail_body_root">
    <div class="container_task_detail_toolbar_root">
      <h1 class="container_task_detail_header_title">任务 {{ taskId }}</h1>
      <span class="container_task_detail_header_conn" :data-state="streamStore.connectionState">
        {{ connectionLabels[streamStore.connectionState] ?? streamStore.connectionState }}
      </span>
    </div>

    <p v-if="loadError" class="container_task_detail_card_error" role="alert">{{ loadError }}</p>

    <section v-if="task" class="container_task_detail_card_summary">
      <p class="container_task_detail_summary_row">
        状态：
        <span class="container_task_detail_summary_status" :data-status="task.status">
          {{ TASK_STATUS_LABELS[task.status] ?? task.status }}
        </span>
        <template v-if="task.currentPhase">（当前阶段：{{ formatPhase(task.currentPhase) }}）</template>
      </p>
      <p class="container_task_detail_summary_row">
        工作流：{{ task.workflowName }} ·
        会话：{{ task.sessionName }} ·
        加速器：{{ task.acceleratorKind.toUpperCase() }} {{ task.acceleratorSpec }}
      </p>
      <p class="container_task_detail_summary_row">
        提交：{{ formatLocalTime(task.queuedAt) }} ·
        开始：{{ formatLocalTime(task.startedAt) }} ·
        结束：{{ formatLocalTime(task.finishedAt) }} ·
        耗时：{{ formatDurationMs(task.durationMs) }}
      </p>
      <p v-if="task.errorMessage" class="container_task_detail_summary_error">
        {{ task.errorMessage }}
      </p>
      <p v-if="task.status === 'succeeded'" class="container_task_detail_summary_row">
        FTP 镜像：
        <template v-if="task.ftpStatus === 'uploaded'">
          已上传到 {{ task.ftpRemotePath }}
        </template>
        <template v-else-if="task.ftpStatus === 'uploading'">上传中…</template>
        <template v-else-if="task.ftpStatus === 'pending'">等待上传</template>
        <template v-else-if="task.ftpStatus === 'failed'">上传失败：{{ task.ftpError }}</template>
        <template v-else>等待加入上传队列</template>
      </p>
      <div v-if="!isTerminal && isOperator" class="container_task_detail_toolbar_cancel">
        <button
          class="container_task_detail_toolbar_interrupt"
          type="button"
          :disabled="actionPending"
          title="重启内核：VM 保留、模型仍在磁盘，可立即重投"
          @click="onCancel('interrupt')"
        >
          中断任务（保留 VM）
        </button>
        <button
          class="container_task_detail_toolbar_destroy"
          type="button"
          :disabled="actionPending"
          title="销毁 VM：停止计费，远端产物全部丢失"
          @click="onCancel('destroy')"
        >
          销毁 VM（产物丢失）
        </button>
      </div>
    </section>

    <section
      v-if="streamStore.driveAuthorizationUrl"
      class="container_task_detail_card_driveauth"
      role="alert"
    >
      <div>
        <h2 class="container_task_detail_driveauth_title">Google Drive 需要授权</h2>
        <p class="container_task_detail_driveauth_help">
          先打开授权页面并完成授权，再返回这里点击“我已完成授权”。
        </p>
      </div>
      <div class="container_task_detail_driveauth_actions">
        <a
          class="container_task_detail_driveauth_open"
          :href="streamStore.driveAuthorizationUrl"
          target="_blank"
          rel="noopener noreferrer"
        >
          打开 Google Drive 授权页面
        </a>
        <button
          class="container_task_detail_driveauth_confirm"
          type="button"
          :disabled="driveConfirmPending || driveConfirmSent"
          @click="onConfirmDrive"
        >
          {{ driveConfirmSent ? '已确认，等待挂载…' : '我已完成授权，继续挂载' }}
        </button>
      </div>
    </section>

    <TaskTimeline :phases="streamStore.phases" />
    <MetricsChart :samples="streamStore.metrics" />
    <LogViewer :lines="streamStore.logLines" />

    <section v-if="task && isTerminal" class="container_task_detail_footer_actions">
      <button
        v-if="task.status === 'succeeded'"
        class="container_task_detail_footer_download"
        type="button"
        :disabled="actionPending"
        @click="onDownload('output')"
      >
        下载产物 ZIP<template v-if="task.outputSizeBytes">（{{ formatBytes(task.outputSizeBytes) }}）</template>
      </button>
      <button
        v-else-if="task.status === 'recoverable'"
        class="container_task_detail_footer_redownload"
        type="button"
        :disabled="actionPending"
        @click="onRedownload"
      >
        重试下载（推理成果可能仍在远端 VM，VM 正在计费）
      </button>
      <button
        v-else-if="task.status === 'failed'"
        class="container_task_detail_footer_runlog"
        type="button"
        :disabled="actionPending"
        @click="onDownload('log')"
      >
        下载 run.log
      </button>
      <p v-else class="container_task_detail_footer_note">任务已结束，无可用动作。</p>
    </section>
  </div>
</template>

<style>
.container_task_detail_body_root {
  max-width: var(--w-page);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_task_detail_toolbar_root {
  display: flex;
  gap: var(--sp-4);
  align-items: baseline;
  margin-bottom: var(--sp-4);
}

.container_task_detail_header_title {
  margin: 0;
  font-size: var(--fs-xl);
  word-break: break-all;
}

.container_task_detail_header_conn {
  flex: none;
  color: var(--c-text-weak);
  font-size: var(--fs-xs);
}

.container_task_detail_header_conn[data-state='open'] {
  color: var(--c-success);
}

.container_task_detail_header_conn[data-state='reconnecting'] {
  color: var(--c-warn);
}

.container_task_detail_card_error {
  padding: var(--sp-4);
  color: var(--c-danger);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_task_detail_card_summary {
  padding: var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_detail_card_driveauth {
  display: flex;
  gap: 20px;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-4);
  margin-bottom: var(--sp-4);
  color: var(--c-warn-text);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-md);
}

.container_task_detail_driveauth_title {
  margin: 0 0 6px;
  font-size: var(--fs-lg);
}

.container_task_detail_driveauth_help {
  margin: 0;
}

.container_task_detail_driveauth_actions {
  display: flex;
  flex: none;
  gap: 10px;
  align-items: center;
}

.container_task_detail_driveauth_open,
.container_task_detail_driveauth_confirm {
  padding: var(--sp-2) 14px;
  font: inherit;
  cursor: pointer;
  border-radius: var(--r-sm);
}

/* 这是个长得像按钮的 <a>。必须显式写 :hover —— main.css 的 `a:hover`
   会把它改成品牌蓝，落在深琥珀底色上等于字消失。 */
.container_task_detail_driveauth_open {
  color: var(--c-text-invert);
  text-decoration: none;
  background: var(--c-warn-text);
  transition: filter var(--t-fast);
}

.container_task_detail_driveauth_open:hover {
  color: var(--c-text-invert);
  filter: brightness(0.9);
}

.container_task_detail_driveauth_confirm {
  color: var(--c-warn-text);
  border: 1px solid var(--c-warn-border);
}

@media (width <= 768px) {
  .container_task_detail_card_driveauth,
  .container_task_detail_driveauth_actions {
    align-items: stretch;
    flex-direction: column;
  }

  .container_task_detail_driveauth_open,
  .container_task_detail_driveauth_confirm {
    text-align: center;
  }
}

.container_task_detail_summary_row {
  margin: var(--sp-1) 0;
}

.container_task_detail_summary_status[data-status='running'] {
  color: var(--c-running);
  font-weight: 600;
}

.container_task_detail_summary_status[data-status='succeeded'] {
  color: var(--c-success);
  font-weight: 600;
}

.container_task_detail_summary_status[data-status='failed'],
.container_task_detail_summary_status[data-status='orphaned'] {
  color: var(--c-danger);
  font-weight: 600;
}

.container_task_detail_summary_status[data-status='recoverable'] {
  color: var(--c-warn-text);
  font-weight: 600;
}

.container_task_detail_summary_error {
  margin: var(--sp-2) 0 0;
  color: var(--c-danger);
}

.container_task_detail_toolbar_cancel {
  display: flex;
  gap: var(--sp-3);
  margin-top: var(--sp-3);
}

.container_task_detail_toolbar_interrupt {
  color: var(--c-warn-text);
  border: 1px solid var(--c-warn);
}

.container_task_detail_toolbar_destroy {
  color: var(--c-danger);
  border: 1px solid var(--c-danger);
}

.container_task_detail_footer_actions {
  padding: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_detail_footer_download,
.container_task_detail_footer_runlog,
.container_task_detail_footer_redownload {
  padding: 10px 20px;
  color: var(--c-text-invert);
  border: none;
}

.container_task_detail_footer_download {
  background: var(--c-brand);
}

.container_task_detail_footer_runlog {
  background: var(--c-text-sub);
}

.container_task_detail_footer_redownload {
  font-weight: 600;
  background: var(--c-warn);
}

.container_task_detail_footer_note {
  margin: 0;
  color: var(--c-text-weak);
}
</style>
