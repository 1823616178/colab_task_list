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
      <div>
        <h1 class="container_dash_stat_header_title">计算任务指挥台</h1>
        <p class="container_dash_stat_header_desc">
          Colab VM 资源分配与长时间计算任务全生命周期实时调度
        </p>
      </div>
      <NuxtLink
        v-if="isOperator"
        class="container_dash_stat_header_action"
        to="/tasks/new"
      >
        + 提交新任务
      </NuxtLink>
    </header>

    <!-- 顶部核心指标看板（doc/09 §3.2） -->
    <section class="container_dash_stat_panel_metrics">
      <div class="container_dash_stat_card_metric">
        <span class="container_dash_stat_cell_label">在线 VM 会话</span>
        <div class="container_dash_stat_cell_value" :data-kind="reachableCount > 0 ? 'active' : 'idle'">
          {{ reachableCount }}
        </div>
        <span class="container_dash_stat_cell_sub">表内登记共 {{ sessionStore.sessions.length }} 台</span>
      </div>

      <div class="container_dash_stat_card_metric">
        <span class="container_dash_stat_cell_label">执行中任务</span>
        <div class="container_dash_stat_cell_value" :data-kind="runningTasks.length + queuedTasks.length > 0 ? 'running' : 'idle'">
          {{ runningTasks.length + queuedTasks.length }}
        </div>
        <span class="container_dash_stat_cell_sub">排队 {{ queuedTasks.length }} · 运行 {{ runningTasks.length }}</span>
      </div>

      <div class="container_dash_stat_card_metric" :data-alert="recoverableTasks.length > 0 ? 'warn' : 'none'">
        <span class="container_dash_stat_cell_label">待重取产物风险</span>
        <div class="container_dash_stat_cell_value" :data-kind="recoverableTasks.length > 0 ? 'warn' : 'idle'">
          {{ recoverableTasks.length }}
        </div>
        <span class="container_dash_stat_cell_sub">
          {{ recoverableTasks.length > 0 ? '⚠️ VM 仍计费，请尽快拉取' : '无未决产物滞留' }}
        </span>
      </div>

      <div class="container_dash_stat_card_metric" :data-alert="orphanCount > 0 ? 'danger' : 'none'">
        <span class="container_dash_stat_cell_label">孤儿会话告警</span>
        <div class="container_dash_stat_cell_value" :data-kind="orphanCount > 0 ? 'danger' : 'idle'">
          {{ orphanCount }}
        </div>
        <span class="container_dash_stat_cell_sub">
          {{ orphanCount > 0 ? '⚠️ 计费空跑中，需立即停止' : '无失联计费 VM' }}
        </span>
      </div>
    </section>

    <!-- 主工作区：左主舞台 + 右侧健康监控与风险待办 -->
    <div class="container_dash_stat_panel_stage">
      <main class="container_dash_stat_panel_main">
        <section class="container_dash_stat_card_running">
          <div class="container_dash_stat_header_sectionwrap">
            <h2 class="container_dash_stat_header_section">
              运行中主舞台（{{ runningTasks.length + queuedTasks.length }}）
            </h2>
            <NuxtLink v-if="runningTasks.length + queuedTasks.length > 0" class="container_dash_stat_toolbar_link" to="/tasks">
              全部任务 →
            </NuxtLink>
          </div>

          <div v-if="loading" class="container_dash_stat_cell_loading" aria-busy="true">
            <span data-sr-only>正在同步计算节点与任务状态…</span>
            <div data-skeleton class="container_dash_stat_cell_skeleton" />
            <div data-skeleton class="container_dash_stat_cell_skeleton" />
          </div>
          <div v-else-if="runningTasks.length === 0 && queuedTasks.length === 0" class="container_dash_list_cell_empty">
            <p>当前没有排队或执行中的计算作业。</p>
            <NuxtLink v-if="isOperator" class="container_dash_stat_toolbar_link" to="/tasks/new">立即配置并投递新任务 →</NuxtLink>
          </div>
          <ul v-else class="container_dash_list_body_list">
            <li
              v-for="task in [...queuedTasks, ...runningTasks]"
              :key="task.id"
              class="container_dash_list_row_item"
              :data-status="task.status"
            >
              <div class="container_dash_list_cell_meta">
                <NuxtLink class="container_dash_list_cell_name" :to="`/tasks/${task.id}`">
                  {{ task.workflowName }}
                </NuxtLink>
                <div class="container_dash_list_cell_tags">
                  <span class="container_dash_list_cell_status" :data-status="task.status">
                    <span class="container_dash_list_cell_pulse" aria-hidden="true" />
                    {{ TASK_STATUS_LABELS[task.status] }}
                  </span>
                  <span class="container_dash_list_cell_session">会话: {{ task.sessionName }}</span>
                  <span v-if="task.currentPhase" class="container_dash_list_cell_phase">
                    阶段: {{ formatPhase(task.currentPhase) }}
                  </span>
                </div>
              </div>
              <button
                v-if="isOperator"
                class="container_dash_list_cell_stop"
                type="button"
                :disabled="stoppingKey !== null"
                title="终止任务并销毁 VM，停止计费；远端产物会丢失"
                @click="stopActiveTask(task)"
              >
                {{ stoppingKey === `task:${task.id}` ? '停止中…' : '终止并销毁 VM' }}
              </button>
            </li>
          </ul>
        </section>
      </main>

      <aside class="container_dash_stat_panel_side">
        <!-- 会话健康 -->
        <section class="container_dash_stat_card_session">
          <h2 class="container_dash_stat_header_section">会话健康度</h2>
          <p v-if="sessionStore.lastError" class="container_dash_stat_cell_error" role="alert">
            {{ sessionStore.lastError }}
          </p>
          <div v-else class="container_dash_stat_cell_summary">
            <p>
              在线 <strong>{{ reachableCount }}</strong> 台 ·
              孤儿 <strong :class="{ 'container_dash_stat_cell_danger': orphanCount > 0 }">{{ orphanCount }}</strong> 台 ·
              登记共 {{ sessionStore.sessions.length }} 条
            </p>
          </div>
          <NuxtLink class="container_dash_stat_toolbar_link" to="/sessions">前往会话管理对账 →</NuxtLink>
        </section>

        <!-- 可重取待办风险 -->
        <section class="container_dash_stat_card_recoverable" :data-haswarn="recoverableTasks.length > 0">
          <h2 class="container_dash_stat_header_section">
            可重取待办（{{ recoverableTasks.length }}）
          </h2>
          <p v-if="recoverableTasks.length === 0" class="container_dash_list_cell_empty">
            没有待取回产物的存活会话。
          </p>
          <ul v-else class="container_dash_list_body_list">
            <li
              v-for="task in recoverableTasks"
              :key="task.id"
              class="container_dash_list_row_item"
              data-status="recoverable"
            >
              <div class="container_dash_list_cell_meta">
                <NuxtLink class="container_dash_list_cell_name" :to="`/tasks/${task.id}`">
                  {{ task.workflowName }}
                </NuxtLink>
                <div class="container_dash_list_cell_hint">
                  VM {{ task.sessionName }} 仍在计费，请及时下载产物或停机
                </div>
              </div>
              <button
                v-if="isOperator"
                class="container_dash_list_cell_stop"
                type="button"
                :disabled="stoppingKey !== null"
                title="销毁可重取任务对应的 VM；未下载产物会丢失"
                @click="stopRecoverableSession(task)"
              >
                {{ stoppingKey === `session:${task.sessionName}` ? '停止中…' : '停机' }}
              </button>
            </li>
          </ul>
        </section>

        <!-- 当前操作员信息 -->
        <section v-if="authStore.user" class="container_dash_stat_card_user">
          <h2 class="container_dash_stat_header_section">操作终端凭据</h2>
          <p>操作员：<strong>{{ authStore.user.username }}</strong>（{{ roleLabels[authStore.user.role] ?? authStore.user.role }}）</p>
          <p class="container_dash_stat_cell_sub">上次登录：{{ formatLocalTime(authStore.user.lastLoginAt) }}</p>
          <p v-if="authStore.user.mustChangePassword" class="container_dash_stat_cell_hint">
            首次登录需修改密码：请在登录页完成修改。
          </p>
        </section>
      </aside>
    </div>
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
  font-size: var(--fs-2xl);
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--c-text);
}

.container_dash_stat_header_desc {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

.container_dash_stat_header_action {
  padding: 8px 18px;
  font-weight: 600;
  color: var(--c-text-invert);
  text-decoration: none;
  background: var(--c-brand);
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast), transform var(--t-fast);
}

.container_dash_stat_header_action:hover {
  filter: brightness(1.1);
  transform: translateY(-1px);
}

/* 顶部指标卡片网格（doc/09 §3.2） */
.container_dash_stat_panel_metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-4);
  margin-bottom: var(--sp-6);
}

.container_dash_stat_card_metric {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
  transition: border-color var(--t-fast), box-shadow var(--t-fast);
}

.container_dash_stat_card_metric[data-alert='warn'] {
  border-color: var(--c-warn);
  box-shadow: var(--sh-glow-warn);
}

.container_dash_stat_card_metric[data-alert='danger'] {
  border-color: var(--c-danger);
  box-shadow: var(--sh-glow-danger);
}

.container_dash_stat_cell_label {
  font-size: var(--fs-xs);
  color: var(--c-text-weak);
  text-transform: uppercase;
}

.container_dash_stat_cell_value {
  font-size: var(--fs-2xl);
  font-weight: 700;
  line-height: 1.2;
  color: var(--c-text);
  font-variant-numeric: tabular-nums;
}

.container_dash_stat_cell_value[data-kind='running'] {
  color: var(--c-brand);
  text-shadow: var(--c-brand-glow-subtle);
}

.container_dash_stat_cell_value[data-kind='warn'] {
  color: var(--c-warn);
}

.container_dash_stat_cell_value[data-kind='danger'] {
  color: var(--c-danger);
}

.container_dash_stat_cell_sub {
  font-size: var(--fs-xs);
  color: var(--c-text-sub);
}

/* 主工作台：左主舞台 + 右侧次面板 */
.container_dash_stat_panel_stage {
  display: grid;
  grid-template-columns: 1fr 380px;
  gap: var(--sp-6);
  align-items: start;
}

.container_dash_stat_card_running {
  padding: var(--sp-6);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-top: 2px solid var(--c-brand);
  border-radius: var(--r-md);
  box-shadow: var(--sh-2);
}

.container_dash_stat_header_sectionwrap {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-4);
}

.container_dash_stat_header_section {
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--c-text);
}

.container_dash_stat_panel_side {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.container_dash_stat_card_session,
.container_dash_stat_card_recoverable,
.container_dash_stat_card_user {
  padding: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_dash_stat_card_recoverable[data-haswarn='true'] {
  border-color: var(--c-warn-border);
  background: var(--c-surface);
}

.container_dash_stat_cell_danger {
  color: var(--c-danger);
}

.container_dash_stat_cell_error {
  margin: 0 0 var(--sp-2);
  color: var(--c-danger);
}

.container_dash_stat_cell_summary {
  margin: 0 0 var(--sp-2);
  color: var(--c-text-sub);
  font-size: var(--fs-sm);
}

.container_dash_stat_cell_summary p {
  margin: 0;
}

.container_dash_stat_toolbar_link {
  font-size: var(--fs-sm);
  color: var(--c-brand);
  text-decoration: none;
}

.container_dash_stat_toolbar_link:hover {
  text-decoration: underline;
}

.container_dash_list_body_list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: 0;
  margin: 0;
  list-style: none;
}

.container_dash_list_row_item {
  display: flex;
  gap: var(--sp-4);
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
  transition: border-color var(--t-fast), box-shadow var(--t-fast);
}

.container_dash_list_row_item:hover {
  border-color: var(--c-border-strong);
}

.container_dash_list_row_item[data-status='recoverable'] {
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.container_dash_list_cell_meta {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
}

.container_dash_list_cell_name {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text);
  text-decoration: none;
}

.container_dash_list_cell_name:hover {
  color: var(--c-brand);
}

.container_dash_list_cell_tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  align-items: center;
}

.container_dash_list_cell_status {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--c-brand);
}

.container_dash_list_cell_pulse {
  width: 6px;
  height: 6px;
  background: var(--c-brand);
  border-radius: 50%;
  box-shadow: 0 0 6px var(--c-brand);
  animation: pulse-dash-dot 1.6s ease-in-out infinite;
}

@keyframes pulse-dash-dot {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }

  50% {
    opacity: 0.3;
    transform: scale(0.8);
  }
}

.container_dash_list_cell_session,
.container_dash_list_cell_phase {
  font-size: var(--fs-xs);
  color: var(--c-text-sub);
}

.container_dash_list_cell_hint {
  font-size: var(--fs-xs);
  color: var(--c-warn-text);
}

.container_dash_list_cell_stop {
  flex: none;
  padding: 6px 12px;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-danger);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
  transition: filter var(--t-fast);
}

.container_dash_list_cell_stop:hover:not(:disabled) {
  filter: brightness(1.15);
}

.container_dash_list_cell_empty {
  margin: 0;
  color: var(--c-text-weak);
  font-size: var(--fs-sm);
}

/* 窄屏响应式 */
.container_dash_stat_cell_loading {
  display: grid;
  gap: var(--sp-3);
}

.container_dash_stat_cell_skeleton {
  height: 64px;
  border-radius: var(--r-md);
}

@media (width <= 960px) {
  .container_dash_stat_panel_metrics {
    grid-template-columns: repeat(2, 1fr);
  }

  .container_dash_stat_panel_stage {
    grid-template-columns: 1fr;
  }
}

@media (width <= 480px) {
  .container_dash_stat_panel_metrics {
    gap: var(--sp-2);
  }

  .container_dash_stat_card_metric {
    padding: var(--sp-3);
  }

  .container_dash_stat_cell_value {
    font-size: var(--fs-2xl);
  }

  .container_dash_stat_header_root {
    flex-direction: column;
    gap: var(--sp-3);
    align-items: stretch;
  }

  .container_dash_stat_header_action {
    text-align: center;
  }
}
</style>
