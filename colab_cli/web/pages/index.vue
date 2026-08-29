<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { TASK_STATUS_LABELS, type TaskRow, useTaskStore } from '~/stores/task'
import { useSessionStore } from '~/stores/session'
import { useUiStore } from '~/stores/ui'

// 总览（doc/04 §2）：运行中/可恢复任务卡片 + 会话健康。
// 孤儿 VM 全局横幅在 app.vue（SessionOrphanBanner），不限于本页。
useHead({ title: '总览' })

const authStore = useAuthStore()
const sessionStore = useSessionStore()
const taskStore = useTaskStore()
const uiStore = useUiStore()

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

const roleLabels: Record<string, string> = {
  admin: '管理员',
  operator: '操作员',
  viewer: '只读',
}

const queuedTasks = ref<TaskRow[]>([])
const runningTasks = ref<TaskRow[]>([])
const recoverableTasks = ref<TaskRow[]>([])
const loading = ref(false)
const stoppingKey = ref<string | null>(null)

const reachableCount = computed(() => sessionStore.sessions.filter(s => s.reachable).length)
const orphanCount = computed(() => sessionStore.orphans.length)

async function loadTasks() {
  const { $apiFetch } = useNuxtApp()
  loading.value = true
  try {
    const [queued, running, recoverable] = await Promise.all([
      $apiFetch<{ items: TaskRow[] }>('/api/tasks', { query: { status: 'queued', size: 10 } }),
      $apiFetch<{ items: TaskRow[] }>('/api/tasks', { query: { status: 'running', size: 10 } }),
      $apiFetch<{ items: TaskRow[] }>('/api/tasks', { query: { status: 'recoverable', size: 10 } }),
    ])
    queuedTasks.value = queued.items
    runningTasks.value = running.items
    recoverableTasks.value = recoverable.items
  } catch (err) {
    uiStore.error(extractApiError(err, '加载总览数据失败'))
  } finally {
    loading.value = false
  }
}

async function stopActiveTask(task: TaskRow) {
  if (!window.confirm(
    `确认停止任务 ${task.id} 并销毁 VM？\n任务会终止、停止计费，远端未取回的产物会丢失。`,
  )) return
  if (!window.confirm('再次确认：VM 和远端产物将被销毁，无法恢复。仍要停止吗？')) return

  stoppingKey.value = `task:${task.id}`
  try {
    await taskStore.cancelTask(task.id, 'destroy')
    uiStore.info('已发起停止任务并销毁 VM')
    await loadTasks()
  } catch (err) {
    uiStore.error(extractApiError(err, '停止任务失败'))
  } finally {
    stoppingKey.value = null
  }
}

async function stopRecoverableSession(task: TaskRow) {
  const session = sessionStore.sessions.find(item =>
    item.name === task.sessionName && item.reachable)
  const runningWarning = session?.hasRunningTask
    ? `\n注意：该 VM 当前还有任务 ${session.runningTask?.id ?? ''} 在运行，也会被终止。`
    : ''
  if (!window.confirm(
    `确认停止会话 ${task.sessionName}？\nVM 将销毁并停止计费，尚未重取的远端产物会丢失。${runningWarning}`,
  )) return
  if (!window.confirm('再次确认：远端产物将无法恢复。仍要停止 VM 吗？')) return

  stoppingKey.value = `session:${task.sessionName}`
  try {
    if (session && !session.runnerNameKnown && session.endpoint) {
      await sessionStore.stopSessionByEndpoint(session.endpoint)
    } else {
      await sessionStore.stopSession(task.sessionName)
    }
    uiStore.info(`已停止会话 ${task.sessionName}`)
    try {
      await sessionStore.reconcile()
    } catch (error) {
      uiStore.warn(extractApiError(error, 'VM 已停止，但任务状态对账暂时失败，请稍后刷新'))
    }
    await loadTasks()
  } catch (err) {
    uiStore.error(extractApiError(err, '停止会话失败'))
  } finally {
    stoppingKey.value = null
  }
}

onMounted(() => {
  void loadTasks()
})
</script>

<template>
  <div class="container_dash_stat_body_root">
    <header class="container_dash_stat_header_root">
      <h1 class="container_dash_stat_header_title">总览</h1>
    </header>

    <section v-if="authStore.user" class="container_dash_stat_card_user">
      <p>当前用户：{{ authStore.user.username }}（{{ roleLabels[authStore.user.role] ?? authStore.user.role }}）</p>
      <p>上次登录：{{ formatLocalTime(authStore.user.lastLoginAt) }}</p>
      <p v-if="authStore.user.mustChangePassword">
        首次登录需修改密码：请退出登录，在登录页完成密码修改。
      </p>
    </section>

    <section class="container_dash_stat_card_session">
      <h2 class="container_dash_stat_header_section">会话健康</h2>
      <p v-if="sessionStore.lastError" class="container_dash_stat_cell_error" role="alert">
        {{ sessionStore.lastError }}
      </p>
      <p v-else class="container_dash_stat_cell_summary">
        在线 {{ reachableCount }} 台
        · 孤儿 {{ orphanCount }} 台
        · 表内 {{ sessionStore.sessions.length }} 条
      </p>
      <NuxtLink class="container_dash_stat_toolbar_link" to="/sessions">查看会话</NuxtLink>
    </section>

    <section class="container_dash_stat_card_running">
      <h2 class="container_dash_stat_header_section">
        运行中（{{ runningTasks.length + queuedTasks.length }}）
      </h2>
      <p v-if="loading" class="container_dash_list_cell_empty">加载中…</p>
      <p v-else-if="runningTasks.length === 0 && queuedTasks.length === 0" class="container_dash_list_cell_empty">
        当前没有排队或运行中的任务。
        <NuxtLink to="/tasks/new">提交任务</NuxtLink>
      </p>
      <ul v-else class="container_dash_list_body_list">
        <li
          v-for="task in [...queuedTasks, ...runningTasks]"
          :key="task.id"
          class="container_dash_list_row_item"
          :data-status="task.status"
        >
          <NuxtLink class="container_dash_list_cell_name" :to="`/tasks/${task.id}`">
            {{ task.workflowName }}
          </NuxtLink>
          <span class="container_dash_list_cell_status">{{ TASK_STATUS_LABELS[task.status] }}</span>
          <span class="container_dash_list_cell_session">{{ task.sessionName }}</span>
          <span class="container_dash_list_cell_phase">{{ formatPhase(task.currentPhase ?? '') }}</span>
          <button
            v-if="isOperator"
            class="container_dash_list_cell_stop"
            type="button"
            :disabled="stoppingKey !== null"
            title="终止任务并销毁 VM，停止计费；远端产物会丢失"
            @click="stopActiveTask(task)"
          >
            {{ stoppingKey === `task:${task.id}` ? '停止中…' : '停止并销毁 VM' }}
          </button>
        </li>
      </ul>
    </section>

    <section class="container_dash_stat_card_recoverable">
      <h2 class="container_dash_stat_header_section">
        可重取（VM 存活，{{ recoverableTasks.length }}）
      </h2>
      <p v-if="recoverableTasks.length === 0" class="container_dash_list_cell_empty">
        没有待重取结果的任务。
      </p>
      <ul v-else class="container_dash_list_body_list">
        <li
          v-for="task in recoverableTasks"
          :key="task.id"
          class="container_dash_list_row_item"
          data-status="recoverable"
        >
          <NuxtLink class="container_dash_list_cell_name" :to="`/tasks/${task.id}`">
            {{ task.workflowName }}
          </NuxtLink>
          <span class="container_dash_list_cell_session">{{ task.sessionName }}</span>
          <span class="container_dash_list_cell_hint">VM 仍在计费，请尽快重取或停止会话</span>
          <button
            v-if="isOperator"
            class="container_dash_list_cell_stop"
            type="button"
            :disabled="stoppingKey !== null"
            title="销毁可重取任务对应的 VM；未下载产物会丢失"
            @click="stopRecoverableSession(task)"
          >
            {{ stoppingKey === `session:${task.sessionName}` ? '停止中…' : '停止 VM' }}
          </button>
        </li>
      </ul>
    </section>
  </div>
</template>

<style>
.container_dash_stat_body_root {
  max-width: var(--w-page);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_dash_stat_header_root {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-6);
}

.container_dash_stat_header_title {
  margin: 0;
  font-size: 22px;
}

.container_dash_stat_header_section {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-lg);
}

.container_dash_stat_card_user,
.container_dash_stat_card_session,
.container_dash_stat_card_running,
.container_dash_stat_card_recoverable {
  padding: var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_dash_stat_card_user p {
  margin: var(--sp-1) 0;
}

.container_dash_stat_cell_error {
  margin: 0 0 var(--sp-2);
  color: var(--c-danger);
}

.container_dash_stat_cell_summary {
  margin: 0 0 var(--sp-2);
  color: var(--c-text-sub);
}

.container_dash_stat_toolbar_link {
  color: var(--c-brand);
  text-decoration: none;
}

.container_dash_list_body_list {
  padding: 0;
  margin: 0;
  list-style: none;
}

.container_dash_list_row_item {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--c-divider);
}

.container_dash_list_row_item[data-status='recoverable'] {
  background: var(--c-warn-bg);
}

.container_dash_list_cell_name {
  font-weight: 600;
  color: var(--c-text);
  text-decoration: none;
}

.container_dash_list_cell_status,
.container_dash_list_cell_session,
.container_dash_list_cell_phase {
  color: var(--c-text-sub);
  font-size: var(--fs-sm);
}

.container_dash_list_cell_hint {
  color: var(--c-danger);
  font-size: var(--fs-sm);
}

.container_dash_list_cell_stop {
  flex: none;
  padding: var(--sp-1) 10px;
  margin-left: auto;
  color: var(--c-text-invert);
  background: var(--c-danger);
  border: 1px solid var(--c-danger);
}

.container_dash_list_cell_empty {
  margin: 0;
  color: var(--c-text-weak);
}
</style>
