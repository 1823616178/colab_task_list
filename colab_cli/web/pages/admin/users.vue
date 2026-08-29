<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import type { AuthUser } from '~/stores/auth'
import { useUiStore } from '~/stores/ui'

// /admin/users 用户管理（admin，doc/04 §2）+ 操作审计只读列表（doc/05 P5）。
// 复用 P2 已有端点：GET/POST /api/users、PATCH /api/users/:id；
// 新建与重置密码的用户 mustChangePassword=true（首次登录强制改密，语义在后端）。
useHead({ title: '用户管理' })

const authStore = useAuthStore()
const uiStore = useUiStore()

const isAdmin = computed(() => authStore.user?.role === 'admin')

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  operator: '操作员',
  viewer: '只读',
}
const ROLE_OPTIONS = ['admin', 'operator', 'viewer'] as const

// ── 用户列表 ────────────────────────────────────────────────
const users = ref<AuthUser[]>([])
const loading = ref(false)
const errorMessage = ref('')

async function fetchUsers() {
  const { $apiFetch } = useNuxtApp()
  loading.value = true
  try {
    const res = await $apiFetch<{ users: AuthUser[] }>('/api/users')
    users.value = res.users
  } catch (err) {
    errorMessage.value = extractApiError(err, '加载用户列表失败')
  } finally {
    loading.value = false
  }
}

// ── 新建用户 ────────────────────────────────────────────────
const newUsername = ref('')
const newPassword = ref('')
const newRole = ref<'admin' | 'operator' | 'viewer'>('viewer')
const creating = ref(false)

async function onCreate() {
  errorMessage.value = ''
  creating.value = true
  try {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch('/api/users', {
      method: 'POST',
      body: { username: newUsername.value.trim(), password: newPassword.value, role: newRole.value },
    })
    uiStore.info(`用户 ${newUsername.value.trim()} 已创建（首次登录须改密）`)
    newUsername.value = ''
    newPassword.value = ''
    newRole.value = 'viewer'
    await fetchUsers()
  } catch (err) {
    errorMessage.value = extractApiError(err, '创建用户失败')
  } finally {
    creating.value = false
  }
}

// ── 改角色 / 停用启用 / 重置密码 ─────────────────────────────
const busyUserId = ref<number | null>(null)

async function patchUser(user: AuthUser, body: Record<string, unknown>, doneText: string) {
  busyUserId.value = user.id
  try {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch(`/api/users/${user.id}`, { method: 'PATCH', body })
    uiStore.info(doneText)
    await fetchUsers()
  } catch (err) {
    uiStore.error(extractApiError(err, '操作失败'))
    await fetchUsers() // 失败也刷新（如角色下拉回显）
  } finally {
    busyUserId.value = null
  }
}

async function onRoleChange(user: AuthUser, event: Event) {
  const role = (event.target as HTMLSelectElement).value
  if (role === user.role) return
  await patchUser(user, { role }, `已将 ${user.username} 的角色改为 ${ROLE_LABELS[role] ?? role}`)
}

async function onToggleActive(user: AuthUser) {
  const next = !user.isActive
  if (!next && !window.confirm(`确认停用 ${user.username}？其登录态将立即失效。`)) return
  await patchUser(user, { isActive: next }, next ? `已启用 ${user.username}` : `已停用 ${user.username}`)
}

async function onResetPassword(user: AuthUser) {
  const password = window.prompt(`为 ${user.username} 设置新密码（至少 8 位；重置后首次登录须改密）：`)
  if (!password) return
  await patchUser(user, { password }, `已重置 ${user.username} 的密码`)
}

// ── 操作审计（只读）─────────────────────────────────────────
interface AuditRow {
  id: string
  userId: number | null
  username: string
  action: string
  target: string | null
  detail: Record<string, unknown> | null
  ip: string
  createdAt: string
}

const auditItems = ref<AuditRow[]>([])
const auditTotal = ref(0)
const auditPage = ref(1)
const auditSize = 20
const auditLoading = ref(false)

const auditTotalPages = computed(() => Math.max(1, Math.ceil(auditTotal.value / auditSize)))

async function fetchAudit() {
  const { $apiFetch } = useNuxtApp()
  auditLoading.value = true
  try {
    const res = await $apiFetch<{ items: AuditRow[], total: number }>(
      `/api/admin/audit?page=${auditPage.value}&size=${auditSize}`,
    )
    auditItems.value = res.items
    auditTotal.value = res.total
  } catch (err) {
    uiStore.error(extractApiError(err, '加载审计日志失败'))
  } finally {
    auditLoading.value = false
  }
}

function goAuditPage(delta: number) {
  const next = auditPage.value + delta
  if (next < 1 || next > auditTotalPages.value) return
  auditPage.value = next
  void fetchAudit()
}

function auditDetailText(row: AuditRow): string {
  if (!row.detail || Object.keys(row.detail).length === 0) return '—'
  const text = JSON.stringify(row.detail)
  return text.length > 120 ? `${text.slice(0, 120)}…` : text
}

onMounted(() => {
  if (!isAdmin.value) return
  void fetchUsers()
  void fetchAudit()
})
</script>

<template>
  <div class="container_user_list_body_root">
    <h1 class="container_user_list_header_title">用户管理</h1>

    <p v-if="!isAdmin" class="container_user_list_form_denied" role="alert">
      仅管理员可访问用户管理。
    </p>

    <template v-else>
      <form class="container_user_create_form_root" @submit.prevent="onCreate">
        <input
          v-model="newUsername"
          class="container_user_create_form_username"
          type="text"
          placeholder="用户名（字母、数字、_ . -）"
          required
        >
        <input
          v-model="newPassword"
          class="container_user_create_form_password"
          type="password"
          placeholder="初始密码（至少 8 位）"
          required
        >
        <select v-model="newRole" class="container_user_create_form_role" aria-label="角色">
          <option v-for="role in ROLE_OPTIONS" :key="role" :value="role">
            {{ ROLE_LABELS[role] }}
          </option>
        </select>
        <button class="container_user_create_form_submit" type="submit" :disabled="creating">
          {{ creating ? '创建中…' : '新建用户' }}
        </button>
      </form>

      <p v-if="errorMessage" class="container_user_list_form_error" data-state="error">
        {{ errorMessage }}
      </p>

      <div class="container_user_list_table_scroll">
        <table class="container_user_list_body_table">
          <thead>
            <tr class="container_user_list_row_head">
              <th class="container_user_list_cell_head">ID</th>
              <th class="container_user_list_cell_head">用户名</th>
              <th class="container_user_list_cell_head">角色</th>
              <th class="container_user_list_cell_head">状态</th>
              <th class="container_user_list_cell_head">创建时间</th>
              <th class="container_user_list_cell_head">最近登录</th>
              <th class="container_user_list_cell_head">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="users.length === 0 && !loading" class="container_user_list_row_item">
              <td class="container_user_list_cell_empty" colspan="7">暂无用户</td>
            </tr>
            <tr
              v-for="user in users"
              :key="user.id"
              class="container_user_list_row_item"
              :data-active="user.isActive ? 'true' : 'false'"
            >
              <td class="container_user_list_cell_id">{{ user.id }}</td>
              <td class="container_user_list_cell_name">
                {{ user.username }}
                <span v-if="user.mustChangePassword" class="container_user_list_cell_flag" title="首次登录须改密">须改密</span>
              </td>
              <td class="container_user_list_cell_role">
                <select
                  class="container_user_list_cell_roleset"
                  :value="user.role"
                  :disabled="busyUserId === user.id"
                  aria-label="修改角色"
                  @change="onRoleChange(user, $event)"
                >
                  <option v-for="role in ROLE_OPTIONS" :key="role" :value="role">
                    {{ ROLE_LABELS[role] }}
                  </option>
                </select>
              </td>
              <td class="container_user_list_cell_active" :data-active="user.isActive ? 'true' : 'false'">
                {{ user.isActive ? '启用' : '已停用' }}
              </td>
              <td class="container_user_list_cell_created">{{ formatLocalTime(user.createdAt) }}</td>
              <td class="container_user_list_cell_login">{{ formatLocalTime(user.lastLoginAt) }}</td>
              <td class="container_user_list_cell_action">
                <button
                  class="container_user_list_cell_toggle"
                  type="button"
                  :disabled="busyUserId === user.id"
                  @click="onToggleActive(user)"
                >
                  {{ user.isActive ? '停用' : '启用' }}
                </button>
                <button
                  class="container_user_list_cell_resetpw"
                  type="button"
                  :disabled="busyUserId === user.id"
                  @click="onResetPassword(user)"
                >
                  重置密码
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="container_user_audit_toolbar_root">
        <h2 class="container_user_audit_header_title">操作审计</h2>
        <button
          class="container_user_audit_toolbar_refresh"
          type="button"
          :disabled="auditLoading"
          @click="fetchAudit"
        >
          {{ auditLoading ? '刷新中…' : '刷新' }}
        </button>
      </div>

      <div class="container_user_audit_table_scroll">
        <table class="container_user_audit_body_table">
          <thead>
            <tr class="container_user_audit_row_head">
              <th class="container_user_audit_cell_head">时间</th>
              <th class="container_user_audit_cell_head">用户</th>
              <th class="container_user_audit_cell_head">动作</th>
              <th class="container_user_audit_cell_head">对象</th>
              <th class="container_user_audit_cell_head">详情</th>
              <th class="container_user_audit_cell_head">IP</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="auditItems.length === 0 && !auditLoading" class="container_user_audit_row_item">
              <td class="container_user_audit_cell_empty" colspan="6">暂无审计记录</td>
            </tr>
            <tr v-for="row in auditItems" :key="row.id" class="container_user_audit_row_item">
              <td class="container_user_audit_cell_time">{{ formatLocalTime(row.createdAt) }}</td>
              <td class="container_user_audit_cell_user">{{ row.username }}</td>
              <td class="container_user_audit_cell_action">{{ row.action }}</td>
              <td class="container_user_audit_cell_target">{{ row.target ?? '—' }}</td>
              <td class="container_user_audit_cell_detail">{{ auditDetailText(row) }}</td>
              <td class="container_user_audit_cell_ip">{{ row.ip || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="container_user_audit_footer_pagination">
        <button
          class="container_user_audit_footer_prev"
          type="button"
          :disabled="auditPage <= 1"
          @click="goAuditPage(-1)"
        >
          上一页
        </button>
        <span class="container_user_audit_footer_pageinfo">
          第 {{ auditPage }} / {{ auditTotalPages }} 页 · 共 {{ auditTotal }} 条
        </span>
        <button
          class="container_user_audit_footer_next"
          type="button"
          :disabled="auditPage >= auditTotalPages"
          @click="goAuditPage(1)"
        >
          下一页
        </button>
      </div>
    </template>
  </div>
</template>

<style>
.container_user_list_table_scroll {
  overflow-x: auto;
}

.container_user_audit_table_scroll {
  overflow-x: auto;
}

.container_user_list_body_root {
  max-width: var(--w-page);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-4);
}

.container_user_list_header_title {
  margin: 0 0 var(--sp-4);
  font-size: var(--fs-xl);
}

.container_user_list_form_denied {
  padding: var(--sp-4);
  color: var(--c-danger);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_user_create_form_root {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}

.container_user_create_form_username,
.container_user_create_form_password,
.container_user_create_form_role {
  padding: var(--sp-2) 10px;
}

.container_user_create_form_submit {
  padding: var(--sp-2) var(--sp-4);
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
}

.container_user_list_form_error {
  margin: 0 0 var(--sp-3);
  color: var(--c-danger);
}

.container_user_list_body_table,
.container_user_audit_body_table {
  width: 100%;
  min-width: 720px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  border-collapse: collapse;
  box-shadow: var(--sh-1);
}

.container_user_list_row_item[data-active='false'] {
  color: var(--c-text-weak);
  background: var(--c-surface-hover);
}

.container_user_list_cell_empty,
.container_user_audit_cell_empty {
  color: var(--c-text-weak);
  text-align: center;
}

.container_user_list_cell_flag {
  padding: 1px 6px;
  margin-left: 6px;
  color: var(--c-warn-text);
  font-size: var(--fs-xs);
  background: var(--c-warn-bg);
  border-radius: var(--r-sm);
}

.container_user_list_cell_roleset {
  padding: var(--sp-1) var(--sp-2);
}

.container_user_list_cell_active[data-active='false'] {
  color: var(--c-danger);
}

.container_user_list_cell_action {
  display: flex;
  gap: var(--sp-2);
}

.container_user_list_cell_toggle,
.container_user_list_cell_resetpw {
  padding: var(--sp-1) 10px;
}

.container_user_list_cell_toggle {
  color: var(--c-danger);
  border-color: var(--c-danger);
}

.container_user_audit_toolbar_root {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin: 28px 0 var(--sp-3);
}

.container_user_audit_header_title {
  margin: 0;
  margin-right: auto;
  font-size: 17px;
}

.container_user_audit_cell_action {
  font-family: monospace;
  font-size: var(--fs-xs);
}

.container_user_audit_cell_detail {
  max-width: 360px;
  overflow: hidden;
  color: var(--c-text-sub);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container_user_audit_footer_pagination {
  display: flex;
  gap: var(--sp-4);
  align-items: center;
  justify-content: center;
  margin-top: var(--sp-4);
}

.container_user_audit_footer_pageinfo {
  color: var(--c-text-sub);
}
</style>
