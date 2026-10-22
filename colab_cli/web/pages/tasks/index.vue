<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { isTerminalStatus, TASK_STATUS_LABELS, useTaskStore } from '~/stores/task'
import type { TaskRow, TaskStatus } from '~/stores/task'
import { backendLabel } from '~/utils/computeBackends'

// /tasks 任务列表（viewer+，doc/04 §2）：状态筛选 + 分页；
// recoverable 行置顶 + 高亮（data-status 表达状态，doc/04 §1.3）——
// 它意味着推理成果还在远端且 VM 持续计费（doc/03 §4），必须醒目。
useHead({ title: '任务列表' })

const taskStore = useTaskStore()
const uiStore = useUiStore()
const authStore = useAuthStore()

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

const statusOptions: { value: string, label: string }[] = [
  { value: '', label: '全部状态' },
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'succeeded', label: '已成功' },
  { value: 'failed', label: '失败' },
  { value: 'recoverable', label: '可重取（VM 存活）' },
  { value: 'canceled', label: '已取消' },
  { value: 'orphaned', label: '已失联' },
]

// 当前页内 recoverable 置顶（服务端按 queuedAt 倒序，页内重排不改变其余相对顺序）
const sortedTasks = computed<TaskRow[]>(() => {
  const recoverable = taskStore.tasks.filter(t => t.status === 'recoverable')
  const rest = taskStore.tasks.filter(t => t.status !== 'recoverable')
  return [...recoverable, ...rest]
})

const recoverableCount = computed(() =>
  taskStore.tasks.filter(t => t.status === 'recoverable').length)

function statusLabel(status: TaskStatus): string {
  return TASK_STATUS_LABELS[status] ?? status
}

function onFilterChange(event: Event) {
  taskStore.setStatusFilter((event.target as HTMLSelectElement).value)
  void taskStore.fetchTasks().catch(err => uiStore.error(extractApiError(err, '加载任务列表失败')))
}

function goPage(delta: number) {
  const next = taskStore.page + delta
  if (next < 1 || next > taskStore.totalPages) return
  taskStore.page = next
  void taskStore.fetchTasks().catch(err => uiStore.error(extractApiError(err, '加载任务列表失败')))
}

function refresh() {
  void taskStore.fetchTasks().catch(err => uiStore.error(extractApiError(err, '加载任务列表失败')))
}

const cancelingId = ref<string | null>(null)
const stoppingId = ref<string | null>(null)

async function onCancel(task: TaskRow) {
  // interrupt 只中断内核、保留 VM；旁边的停止按钮走 destroy 并二次确认。
  if (!window.confirm(`确认取消任务 ${task.id}？\n将中断内核执行，VM 保留、可稍后重投。`)) return
  cancelingId.value = task.id
  try {
    await taskStore.cancelTask(task.id, 'interrupt')
    uiStore.info('已发起取消')
    refresh()
  } catch (err) {
    uiStore.error(extractApiError(err, '取消失败'))
  } finally {
    cancelingId.value = null
  }
}

async function onStop(task: TaskRow) {
  if (!window.confirm(
    `确认停止任务 ${task.id} 并销毁 VM？\n任务会终止、停止计费，远端未取回的产物会丢失。`,
  )) return
  if (!window.confirm('再次确认：VM 和远端产物将被销毁，无法恢复。仍要停止吗？')) return

  stoppingId.value = task.id
  try {
    await taskStore.cancelTask(task.id, 'destroy')
    uiStore.info('已发起停止任务并销毁 VM')
    refresh()
  } catch (err) {
    uiStore.error(extractApiError(err, '停止任务失败'))
  } finally {
    stoppingId.value = null
  }
}

onMounted(refresh)
</script>

<template>
  <div class="container_task_list_body_root">
    <div class="container_task_list_toolbar_root">
      <h1 class="container_task_list_header_title">任务列表</h1>
      <select
        class="container_task_list_filter_status"
        :value="taskStore.statusFilter"
        aria-label="按状态筛选"
        @change="onFilterChange"
      >
        <option v-for="opt in statusOptions" :key="opt.value" :value="opt.value">
          {{ opt.label }}
        </option>
      </select>
      <button
        class="container_task_list_toolbar_refresh"
        type="button"
        :disabled="taskStore.loading"
        @click="refresh"
      >
        {{ taskStore.loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <p v-if="recoverableCount > 0" class="container_task_list_card_recoverable" role="alert">
      有 {{ recoverableCount }} 个任务处于「可重取」状态：对应远端 VM 仍存活且正在持续计费，
      请尽快进入详情页重试下载。
    </p>

    <div class="container_task_list_table_scroll">
      <table class="container_task_list_body_table">
        <thead>
          <tr class="container_task_list_row_head">
            <th class="container_task_list_cell_head">任务 ID</th>
            <th class="container_task_list_cell_head">工作流</th>
            <th class="container_task_list_cell_head">状态</th>
            <th class="container_task_list_cell_head">会话</th>
            <th class="container_task_list_cell_head">加速器</th>
            <th class="container_task_list_cell_head">提交时间</th>
            <th class="container_task_list_cell_head">耗时</th>
            <th class="container_task_list_cell_head">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="sortedTasks.length === 0 && !taskStore.loading" class="container_task_list_row_item">
            <td class="container_task_list_cell_empty" colspan="8">暂无任务</td>
          </tr>
          <tr
            v-for="task in sortedTasks"
            :key="task.id"
            class="container_task_list_row_item"
            :data-status="task.status"
          >
            <td class="container_task_list_cell_id" data-label="任务 ID">
              <NuxtLink :to="`/tasks/${task.id}`">{{ task.id }}</NuxtLink>
            </td>
            <td class="container_task_list_cell_workflow" data-label="工作流">{{ task.workflowName }}</td>
            <td class="container_task_list_cell_status" data-label="状态">
              <span class="container_task_list_cell_badge" :data-status="task.status">
                {{ statusLabel(task.status) }}
              </span>
            </td>
            <td class="container_task_list_cell_session" data-label="会话">{{ task.sessionName }}</td>
            <td class="container_task_list_cell_gpu" data-label="加速器">
              {{ backendLabel(task.backend) }} · {{ task.acceleratorKind.toUpperCase() }} {{ task.acceleratorSpec }}
            </td>
            <td class="container_task_list_cell_time" data-label="提交时间">{{ formatLocalTime(task.queuedAt) }}</td>
            <td class="container_task_list_cell_duration" data-label="耗时">{{ formatDurationMs(task.durationMs) }}</td>
            <td class="container_task_list_cell_action" data-label="操作">
              <div v-if="!isTerminalStatus(task.status) && isOperator" class="container_task_list_cell_actiongroup">
                <button
                  class="container_task_list_cell_cancel"
                  type="button"
                  :disabled="cancelingId === task.id || stoppingId === task.id"
                  title="中断内核执行，但保留 VM；VM 可能继续计费"
                  @click="onCancel(task)"
                >
                  {{ cancelingId === task.id ? '中断中…' : '中断（保留 VM）' }}
                </button>
                <button
                  class="container_task_list_cell_stop"
                  type="button"
                  :disabled="cancelingId === task.id || stoppingId === task.id"
                  title="终止任务并销毁 VM，停止计费；远端产物会丢失"
                  @click="onStop(task)"
                >
                  {{ stoppingId === task.id ? '停止中…' : '停止并销毁 VM' }}
                </button>
              </div>
              <NuxtLink v-else class="container_task_list_cell_detail" :to="`/tasks/${task.id}`">
                详情
              </NuxtLink>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="container_task_list_footer_pagination">
      <button
        class="container_task_list_footer_prev"
        type="button"
        :disabled="taskStore.page <= 1"
        @click="goPage(-1)"
      >
        上一页
      </button>
      <span class="container_task_list_footer_pageinfo">
        第 {{ taskStore.page }} / {{ taskStore.totalPages }} 页 · 共 {{ taskStore.total }} 条
      </span>
      <button
        class="container_task_list_footer_next"
        type="button"
        :disabled="taskStore.page >= taskStore.totalPages"
        @click="goPage(1)"
      >
        下一页
      </button>
    </div>
  </div>
</template>

<style>
.container_task_list_table_scroll {
  overflow-x: auto;
}

.container_task_list_body_root {
  max-width: var(--w-page);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_task_list_toolbar_root {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin-bottom: var(--sp-4);
}

.container_task_list_header_title {
  margin: 0;
  margin-right: auto;
  font-size: var(--fs-xl);
}

.container_task_list_filter_status {
  padding: 6px 12px;
  font-size: var(--fs-sm);
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_task_list_card_recoverable {
  padding: var(--sp-3) var(--sp-4);
  margin: 0 0 var(--sp-4);
  color: var(--c-warn-text);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-glow-warn);
}

.container_task_list_body_table {
  width: 100%;
  min-width: 720px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  border-collapse: collapse;
  box-shadow: var(--sh-1);
}

.container_task_list_row_head th {
  vertical-align: middle;
}

.container_task_list_row_item,
.container_task_list_row_item td {
  vertical-align: middle;
}

.container_task_list_row_item[data-status='recoverable'] {
  background: var(--c-warn-bg);
  box-shadow: inset 3px 0 0 var(--c-warn);
}

.container_task_list_cell_id a {
  font-family: var(--ff-mono);
  font-weight: 600;
  color: var(--c-brand);
}

.container_task_list_cell_empty {
  color: var(--c-text-weak);
  text-align: center;
  vertical-align: middle;
}

.container_task_list_cell_status {
  vertical-align: middle;
}

.container_task_list_cell_badge {
  display: inline-block;
  padding: 2px 8px;
  font-size: var(--fs-xs);
  font-weight: 600;
  border-radius: var(--r-sm);
  vertical-align: middle;
}

.container_task_list_cell_badge[data-status='running'] {
  color: var(--c-running);
  background: var(--c-running-bg);
}

.container_task_list_cell_badge[data-status='succeeded'] {
  color: var(--c-success);
  background: var(--c-success-bg);
}

.container_task_list_cell_badge[data-status='failed'],
.container_task_list_cell_badge[data-status='orphaned'] {
  color: var(--c-danger);
  background: var(--c-danger-bg);
}

.container_task_list_cell_badge[data-status='recoverable'] {
  color: var(--c-warn);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
}

.container_task_list_cell_detail {
  display: inline-flex;
  align-items: center;
}

.container_task_list_cell_cancel {
  padding: var(--sp-1) 10px;
  color: var(--c-warn-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-warn);
  border-radius: var(--r-sm);
}

.container_task_list_cell_actiongroup {
  display: flex;
  gap: 6px;
  align-items: center;
}

.container_task_list_cell_stop {
  padding: var(--sp-1) 10px;
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-danger);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
}

.container_task_list_footer_pagination {
  display: flex;
  gap: var(--sp-4);
  align-items: center;
  justify-content: center;
  margin-top: var(--sp-4);
}

.container_task_list_footer_pageinfo {
  color: var(--c-text-sub);
}

.container_task_list_cell_badge[data-status='queued'],
.container_task_list_cell_badge[data-status='canceled'] {
  color: var(--c-text-sub);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
}

/* 窄屏：表格改为卡片，每个单元格用 data-label 作为字段名 */
@media (width <= 768px) {
  .container_task_list_toolbar_root {
    flex-wrap: wrap;
  }

  .container_task_list_body_table {
    min-width: 0;
    background: transparent;
    border: none;
    box-shadow: none;
  }

  .container_task_list_body_table thead {
    display: none;
  }

  .container_task_list_body_table tbody,
  .container_task_list_body_table tr {
    display: block;
  }

  .container_task_list_row_item {
    margin-bottom: var(--sp-3);
    padding: var(--sp-2) var(--sp-3);
    background: var(--c-surface);
    border: 1px solid var(--c-border);
    border-radius: var(--r-md);
    box-shadow: var(--sh-1);
  }

  .container_task_list_row_item td {
    display: flex;
    gap: var(--sp-3);
    justify-content: space-between;
    padding: 6px 0;
    text-align: right;
    border-bottom: 1px dashed var(--c-divider);
  }

  .container_task_list_row_item td:last-child {
    border-bottom: none;
  }

  .container_task_list_row_item td[data-label]::before {
    flex: none;
    font-size: var(--fs-xs);
    color: var(--c-text-weak);
    text-align: left;
    content: attr(data-label);
  }

  .container_task_list_cell_actiongroup {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
}
</style>
