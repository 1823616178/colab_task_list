<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import type { TaskRow } from '~/stores/task'
import { useUiStore } from '~/stores/ui'

useHead({ title: '历史产物' })

const authStore = useAuthStore()
const uiStore = useUiStore()
const apiFetch = useNuxtApp().$apiFetch
const items = ref<TaskRow[]>([])
const loading = ref(false)
const page = ref(1)
const size = 20
const total = ref(0)
const downloadingId = ref<string | null>(null)
const retryingId = ref<string | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))
const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')
const hasPendingUpload = computed(() =>
  items.value.some(item => item.ftpStatus === 'pending' || item.ftpStatus === 'uploading'))

function ftpStatusText(task: TaskRow): string {
  if (task.ftpStatus === 'uploaded') return `已上传：${task.ftpRemotePath ?? ''}`
  if (task.ftpStatus === 'uploading') return `上传中（第 ${task.ftpAttempts} 次）`
  if (task.ftpStatus === 'pending') return '等待上传'
  if (task.ftpStatus === 'failed') return `失败：${task.ftpError ?? '未知错误'}`
  return '等待加入队列'
}

async function loadHistory(showError = true) {
  loading.value = true
  try {
    const response = await apiFetch<{
      items: TaskRow[]
      total: number
      page: number
      size: number
    }>('/api/history', { query: { page: page.value, size } })
    items.value = response.items
    total.value = response.total
  } catch (error) {
    if (showError) uiStore.error(extractApiError(error, '加载历史产物失败'))
  } finally {
    loading.value = false
    schedulePoll()
  }
}

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  if (!hasPendingUpload.value) return
  pollTimer = setTimeout(() => void loadHistory(false), 3_000)
}

async function download(task: TaskRow) {
  downloadingId.value = task.id
  try {
    await downloadApiFile(`/api/tasks/${task.id}/output`, `output-${task.id}.zip`)
  } catch (error) {
    uiStore.error(error instanceof Error ? error.message : '下载失败')
  } finally {
    downloadingId.value = null
  }
}

async function retryUpload(task: TaskRow) {
  retryingId.value = task.id
  try {
    await apiFetch(`/api/history/${task.id}/retry`, { method: 'POST' })
    uiStore.info('已重新加入 FTP 上传队列')
    await loadHistory()
  } catch (error) {
    uiStore.error(extractApiError(error, '重新上传失败'))
  } finally {
    retryingId.value = null
  }
}

function goPage(delta: number) {
  const next = page.value + delta
  if (next < 1 || next > totalPages.value) return
  page.value = next
  void loadHistory()
}

onMounted(() => void loadHistory())
onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
})
</script>

<template>
  <main class="container_task_history_body_root">
    <div class="container_task_history_toolbar_root">
      <h1 class="container_task_history_header_title">历史产物</h1>
      <button type="button" :disabled="loading" @click="loadHistory()">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>
    <p class="container_task_history_header_help">
      仅显示真实运行成功且已下载到本机的产物；FTP 上传在后台进行，不影响本地下载。
    </p>
    <div class="container_task_history_table_scroll">
      <table class="container_task_history_body_table">
        <thead>
          <tr>
            <th>任务</th>
            <th>工作流</th>
            <th>完成时间</th>
            <th>大小</th>
            <th>FTP 镜像</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="items.length === 0 && !loading">
            <td colspan="6" class="container_task_history_cell_empty">暂无成功产物</td>
          </tr>
          <tr v-for="task in items" :key="task.id">
            <td><NuxtLink :to="`/tasks/${task.id}`">{{ task.id }}</NuxtLink></td>
            <td>{{ task.workflowName }}</td>
            <td>{{ formatLocalTime(task.finishedAt) }}</td>
            <td>{{ formatBytes(task.outputSizeBytes) }}</td>
            <td class="container_task_history_cell_ftp" :data-status="task.ftpStatus ?? 'pending'">
              {{ ftpStatusText(task) }}
            </td>
            <td class="container_task_history_cell_actions">
              <button
                type="button"
                :disabled="downloadingId === task.id"
                @click="download(task)"
              >
                {{ downloadingId === task.id ? '下载中…' : '下载' }}
              </button>
              <button
                v-if="['failed', 'uploading'].includes(task.ftpStatus ?? '') && isOperator"
                type="button"
                :disabled="retryingId === task.id"
                @click="retryUpload(task)"
              >
                {{ retryingId === task.id ? '入队中…' : '重试 FTP' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="container_task_history_footer_pagination">
      <button type="button" :disabled="page <= 1" @click="goPage(-1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
      <button type="button" :disabled="page >= totalPages" @click="goPage(1)">下一页</button>
    </div>
  </main>
</template>

<style>
.container_task_history_body_root {
  max-width: var(--w-page);
  padding: var(--sp-6) var(--sp-4);
  margin: 0 auto;
}

.container_task_history_toolbar_root,
.container_task_history_footer_pagination,
.container_task_history_cell_actions {
  display: flex;
  gap: 10px;
  align-items: center;
}

.container_task_history_toolbar_root {
  justify-content: space-between;
}

.container_task_history_header_title {
  margin: 0;
  font-size: var(--fs-xl);
}

.container_task_history_header_help {
  color: var(--c-text-sub);
}

.container_task_history_table_scroll {
  overflow-x: auto;
}

.container_task_history_body_table {
  width: 100%;
  min-width: 720px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  border-collapse: collapse;
  box-shadow: var(--sh-1);
}

.container_task_history_cell_ftp[data-status='uploaded'] {
  color: var(--c-success);
}

.container_task_history_cell_ftp[data-status='failed'] {
  color: var(--c-danger);
}

.container_task_history_cell_ftp[data-status='pending'],
.container_task_history_cell_ftp[data-status='uploading'] {
  color: var(--c-warn-text);
}

.container_task_history_cell_empty {
  color: var(--c-text-weak);
  text-align: center !important;
}

.container_task_history_footer_pagination {
  justify-content: center;
  margin-top: var(--sp-4);
}
</style>
