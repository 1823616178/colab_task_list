<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { useSessionStore } from '~/stores/session'
import type { SessionItem } from '~/stores/session'
import { useUiStore } from '~/stores/ui'

// /sessions 会话页（operator+ 操作，viewer 只读，doc/04 §2）：
// Runner 实况 ⋈ sessions 表 ⋈ 在途任务的聚合视图；孤儿高亮 + 一键停止
// （含名称映射丢失时按 endpoint 停止）；VM 存活时长与预估消耗
// （近似费率表见 utils/cost.ts，以 Colab 账单为准）。
useHead({ title: '会话' })

const authStore = useAuthStore()
const sessionStore = useSessionStore()
const uiStore = useUiStore()

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

// 存活时长按秒走动：仅本地时钟，不发请求（列表数据本身由 store 30s 轮询刷新）
const nowTick = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  tickTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
  void sessionStore.fetchSessions().catch(() => {})
})
onUnmounted(() => {
  if (tickTimer) clearInterval(tickTimer)
})

type RowState = 'orphan' | 'alive' | 'unreachable' | 'stopped' | 'unknown'

function rowState(item: SessionItem): RowState {
  if (item.isOrphan) return 'orphan'
  if (item.reachable) return 'alive'
  if (item.dbStatus === 'unreachable') return 'unreachable'
  if (item.dbStatus === 'stopped') return 'stopped'
  return 'unknown'
}

function statusText(item: SessionItem): string {
  if (item.isOrphan) return '孤儿（在计费）'
  if (item.reachable) return item.runnerStatus === 'BUSY' ? '运行中' : '空闲'
  if (item.dbStatus === 'unreachable') return '失联'
  if (item.dbStatus === 'stopped') return '已停止'
  if (item.hasRunningTask) return '未确认（任务在途）'
  return '不在线'
}

function aliveMs(item: SessionItem): number | null {
  if (!item.createdAt) return null
  const start = new Date(item.createdAt).getTime()
  const end = item.stoppedAt ? new Date(item.stoppedAt).getTime() : nowTick.value
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null
  return end - start
}

function costText(item: SessionItem): string {
  return formatComputeUnits(estimateComputeUnits(item.variant, item.accelerator, aliveMs(item)))
}

function acceleratorText(item: SessionItem): string {
  const kind = (item.accelerator ?? '').toUpperCase()
  return [kind || null, item.variant].filter(Boolean).join(' ') || '—'
}

/** Runner 名称映射丢失时，即使 DB 恢复了显示名，也只能按 endpoint 停止。 */
function hasUsableName(item: SessionItem): boolean {
  return item.runnerNameKnown
}

function refresh() {
  void sessionStore.fetchSessions().catch(err => uiStore.error(extractApiError(err, '加载会话列表失败')))
}

const reconciling = ref(false)

async function onReconcile() {
  reconciling.value = true
  try {
    const result = await sessionStore.reconcile()
    const unreachable = result.gone.filter(g => g.status === 'unreachable').length
    uiStore.info(
      `对账完成：活 ${result.alive.length}，孤儿 ${result.orphans.length}，`
        + `失联 ${unreachable}，转 orphaned 任务 ${result.orphanedTasks.length}，`
        + `清理失效可重取 ${result.expiredRecoveries.length}`,
    )
  } catch (err) {
    uiStore.error(extractApiError(err, '对账失败'))
  } finally {
    reconciling.value = false
  }
}

const GPU_SPECS = ['T4', 'L4', 'G4', 'H100', 'A100']
const TPU_SPECS = ['v5e1', 'v6e1']
const SESSION_NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/

const newName = ref('comfyui-h3')
const newKind = ref<'gpu' | 'tpu'>('gpu')
const newSpec = ref('T4')
const newMountDrive = ref(true)
const starting = ref(false)
const specOptions = computed(() => (newKind.value === 'gpu' ? GPU_SPECS : TPU_SPECS))
watch(newKind, (kind) => {
  newSpec.value = kind === 'gpu' ? GPU_SPECS[0]! : TPU_SPECS[0]!
})

interface OpLogLine {
  ts: string
  action: string
  ok: boolean
  text: string
}
const opLog = ref<OpLogLine[]>([])

function appendLog(action: string, ok: boolean, text: string) {
  const stamp = new Date().toLocaleTimeString()
  opLog.value = [{ ts: stamp, action, ok, text: text.trim() || '（无输出）' }, ...opLog.value].slice(0, 30)
}

const stoppingKey = ref<string | null>(null)

async function onStart() {
  const name = newName.value.trim()
  if (!SESSION_NAME_PATTERN.test(name)) {
    uiStore.error('会话名仅允许字母、数字、_ . -，最长 128 字符')
    return
  }
  if (!window.confirm(`确认启动会话 ${name}（${newKind.value.toUpperCase()} ${newSpec.value}）？\n将创建 Colab VM 并开始计费，可能需要一两分钟。`)) return
  starting.value = true
  try {
    const result = await sessionStore.createSession({
      name,
      accelerator: { kind: newKind.value, spec: newSpec.value },
      mount_drive: newMountDrive.value,
    })
    appendLog('启动', true, result.log || `会话 ${name} 已启动`)
    uiStore.info(`会话 ${name} 已启动`)
  } catch (err) {
    const message = extractApiError(err, '启动会话失败')
    appendLog('启动', false, message)
    uiStore.error(message)
  } finally {
    starting.value = false
  }
}

async function onStop(item: SessionItem) {
  if (!window.confirm(`确认停止会话 ${item.name}？\nVM 将销毁、停止计费，远端未取回的产物会丢失。`)) return
  stoppingKey.value = item.name
  try {
    const result = await sessionStore.stopSession(item.name)
    appendLog('停止', true, result.log || `会话 ${item.name} 已停止`)
    uiStore.info(`已停止会话 ${item.name}`)
  } catch (err) {
    const message = extractApiError(err, '停止会话失败')
    appendLog('停止', false, message)
    uiStore.error(message)
  } finally {
    stoppingKey.value = null
  }
}

async function onStopByEndpoint(item: SessionItem) {
  if (!item.endpoint) return
  if (!window.confirm(`该会话名称映射丢失（endpoint: ${item.endpoint}）。\n确认按 endpoint 停止？VM 将销毁、停止计费。`)) return
  stoppingKey.value = item.endpoint
  try {
    const result = await sessionStore.stopSessionByEndpoint(item.endpoint)
    appendLog('按 endpoint 停止', true, result.log || `endpoint ${item.endpoint} 已停止`)
    uiStore.info('已按 endpoint 停止会话')
  } catch (err) {
    const message = extractApiError(err, '按 endpoint 停止失败')
    appendLog('按 endpoint 停止', false, message)
    uiStore.error(message)
  } finally {
    stoppingKey.value = null
  }
}
</script>

<template>
  <div class="container_session_list_body_root">
    <div class="container_session_list_toolbar_root">
      <h1 class="container_session_list_header_title">会话</h1>
      <button
        v-if="isOperator"
        class="container_session_list_toolbar_reconcile"
        type="button"
        :disabled="reconciling"
        @click="onReconcile"
      >
        {{ reconciling ? '对账中…' : '手动对账' }}
      </button>
      <button
        class="container_session_list_toolbar_refresh"
        type="button"
        :disabled="sessionStore.loading"
        @click="refresh"
      >
        {{ sessionStore.loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <form
      v-if="isOperator"
      class="container_session_create_form_root"
      @submit.prevent="onStart"
    >
      <h2 class="container_session_create_header_title">启动 Colab 会话</h2>
      <input
        v-model="newName"
        class="container_session_create_form_name"
        type="text"
        maxlength="128"
        placeholder="会话名"
        aria-label="会话名"
        required
      >
      <select v-model="newKind" class="container_session_create_form_kind" aria-label="加速器类型">
        <option value="gpu">GPU</option>
        <option value="tpu">TPU</option>
      </select>
      <select v-model="newSpec" class="container_session_create_form_spec" aria-label="规格">
        <option v-for="spec in specOptions" :key="spec" :value="spec">{{ spec }}</option>
      </select>
      <label class="container_session_create_form_drive">
        <input v-model="newMountDrive" type="checkbox">
        挂载 Drive
      </label>
      <button
        class="container_session_create_footer_start"
        type="submit"
        :disabled="starting"
      >
        {{ starting ? '启动中…' : '启动' }}
      </button>
    </form>

    <p v-if="sessionStore.orphans.length > 0" class="container_session_list_card_orphan" role="alert">
      检出 {{ sessionStore.orphans.length }} 个孤儿会话（VM 仍在计费但无人认领），请尽快停止：
      {{ sessionStore.orphans.map(o => o.name).join('、') }}
    </p>

    <p v-if="sessionStore.lastError" class="container_session_list_card_error" role="alert">
      {{ sessionStore.lastError }}（Runner 不可达时列表不可用，已停止的会话记录不受影响）
    </p>

    <div class="container_session_list_table_scroll">
      <table class="container_session_list_body_table">
        <thead>
          <tr class="container_session_list_row_head">
            <th class="container_session_list_cell_head">会话</th>
            <th class="container_session_list_cell_head">状态</th>
            <th class="container_session_list_cell_head">加速器</th>
            <th class="container_session_list_cell_head">运行中任务</th>
            <th class="container_session_list_cell_head">存活时长</th>
            <th class="container_session_list_cell_head">预估消耗</th>
            <th class="container_session_list_cell_head">最近在线</th>
            <th class="container_session_list_cell_head">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="sessionStore.sessions.length === 0" class="container_session_list_row_item">
            <td class="container_session_list_cell_empty" colspan="8">
              {{ sessionStore.lastError ? '列表不可用' : '暂无会话' }}
            </td>
          </tr>
          <tr
            v-for="item in sessionStore.sessions"
            :key="item.name"
            class="container_session_list_row_item"
            :data-status="rowState(item)"
          >
            <td class="container_session_list_cell_name" :title="item.endpoint ?? ''">
              {{ item.name }}
            </td>
            <td class="container_session_list_cell_status">
              <span class="container_session_list_cell_badge" :data-status="rowState(item)">
                {{ statusText(item) }}
              </span>
            </td>
            <td class="container_session_list_cell_gpu">{{ acceleratorText(item) }}</td>
            <td class="container_session_list_cell_task">
              <NuxtLink v-if="item.runningTask" :to="`/tasks/${item.runningTask.id}`">
                {{ item.runningTask.workflowName }}
              </NuxtLink>
              <template v-else>—</template>
            </td>
            <td class="container_session_list_cell_uptime">{{ formatDurationMs(aliveMs(item)) }}</td>
            <td class="container_session_list_cell_cost">{{ costText(item) }}</td>
            <td class="container_session_list_cell_seen">{{ formatLocalTime(item.lastSeenAt) }}</td>
            <td class="container_session_list_cell_action">
              <template v-if="isOperator && item.reachable">
                <button
                  v-if="hasUsableName(item)"
                  class="container_session_list_cell_stop"
                  type="button"
                  :disabled="stoppingKey === item.name"
                  @click="onStop(item)"
                >
                  停止
                </button>
                <button
                  v-else
                  class="container_session_list_cell_stop"
                  type="button"
                  :disabled="stoppingKey === item.endpoint"
                  @click="onStopByEndpoint(item)"
                >
                  按 Endpoint 停止
                </button>
              </template>
              <template v-else>—</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <section class="container_session_list_panel_log">
      <h2 class="container_session_list_header_log">状态日志</h2>
      <p v-if="opLog.length === 0" class="container_session_list_cell_empty">
        启动或停止会话后，这里会显示 colab 命令输出。
      </p>
      <pre
        v-for="(line, index) in opLog"
        :key="`${line.ts}-${index}`"
        class="container_session_list_panel_logtext"
        :data-ok="line.ok ? 'true' : 'false'"
      >[{{ line.ts }}] {{ line.action }}
{{ line.text }}</pre>
    </section>

    <p class="container_session_list_footer_note">
      存活时长以「首次对账见到该 VM」为起点（Runner 不暴露 VM 创建时间，属近似值）；
      预估消耗 = 近似费率 × 存活时长，仅供量级感知，<strong>以 Colab 账单为准</strong>。
      <span v-if="sessionStore.fetchedAt">最近刷新：{{ formatLocalTime(sessionStore.fetchedAt) }}</span>
    </p>
  </div>
</template>

<style>
.container_session_list_table_scroll {
  overflow-x: auto;
}

.container_session_list_body_root {
  max-width: var(--w-page);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_session_list_toolbar_root {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin-bottom: var(--sp-4);
}

.container_session_list_header_title {
  margin: 0;
  margin-right: auto;
  font-size: var(--fs-xl);
}

.container_session_list_card_orphan {
  padding: var(--sp-3) var(--sp-4);
  margin: 0 0 var(--sp-4);
  color: var(--c-danger-hover);
  font-weight: 600;
  background: var(--c-danger-bg);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-md);
}

.container_session_list_card_error {
  padding: var(--sp-3) var(--sp-4);
  margin: 0 0 var(--sp-4);
  color: var(--c-warn-text);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-md);
}

.container_session_list_body_table {
  width: 100%;
  min-width: 720px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  border-collapse: collapse;
  box-shadow: var(--sh-1);
}

.container_session_list_row_head th {
  vertical-align: middle;
}

.container_session_list_row_item,
.container_session_list_row_item td {
  vertical-align: middle;
}

.container_session_list_row_item[data-status='orphan'] {
  background: var(--c-danger-bg);
  box-shadow: inset 3px 0 0 var(--c-danger);
}

.container_session_list_cell_empty {
  color: var(--c-text-weak);
  text-align: center;
  vertical-align: middle;
}

.container_session_list_cell_status {
  vertical-align: middle;
}

.container_session_list_cell_badge {
  display: inline-block;
  padding: 2px 8px;
  font-size: var(--fs-xs);
  font-weight: 600;
  border-radius: var(--r-sm);
  vertical-align: middle;
}

.container_session_list_cell_badge[data-status='alive'] {
  color: var(--c-success);
  background: var(--c-success-bg);
}

.container_session_list_cell_badge[data-status='orphan'] {
  color: var(--c-danger);
  background: var(--c-danger-bg);
  border: 1px solid var(--c-danger);
}

.container_session_list_cell_badge[data-status='unreachable'] {
  color: var(--c-danger);
  background: var(--c-danger-bg);
}

.container_session_list_cell_badge[data-status='stopped'] {
  color: var(--c-text-weak);
  background: var(--c-surface-sub);
}

.container_session_list_cell_name {
  font-family: var(--ff-mono);
  font-weight: 600;
  color: var(--c-text);
}

.container_session_list_cell_stop {
  padding: var(--sp-1) 10px;
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-danger);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
  transition: filter var(--t-fast);
}

.container_session_list_cell_stop:hover:not(:disabled) {
  filter: brightness(1.15);
}

.container_session_list_footer_note {
  margin-top: var(--sp-3);
  color: var(--c-text-weak);
  font-size: var(--fs-sm);
}

.container_session_create_form_root {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_session_create_header_title {
  width: 100%;
  margin: 0 0 var(--sp-1);
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text);
}

.container_session_create_form_name,
.container_session_create_form_kind,
.container_session_create_form_spec {
  padding: var(--sp-2) 10px;
}

.container_session_create_form_name {
  min-width: 180px;
}

.container_session_create_form_drive {
  display: flex;
  gap: 6px;
  align-items: center;
  color: var(--c-text-sub);
  font-size: var(--fs-md);
}

.container_session_create_footer_start {
  padding: var(--sp-2) var(--sp-4);
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast);
}

.container_session_create_footer_start:hover:not(:disabled) {
  filter: brightness(1.1);
}

.container_session_list_panel_log {
  padding: var(--sp-3) var(--sp-4);
  margin-top: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_session_list_header_log {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text);
}

.container_session_list_panel_logtext {
  max-height: 220px;
  padding: var(--sp-2) 10px;
  margin: 0 0 var(--sp-2);
  overflow: auto;
  color: var(--c-log-text);
  font-family: var(--ff-mono);
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  background: var(--c-log-bg);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_session_list_panel_logtext[data-ok='false'] {
  color: var(--c-danger);
  background: var(--c-danger-bg);
}
</style>
