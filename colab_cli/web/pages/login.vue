<script setup lang="ts">
import type { Ref } from 'vue'
import { extractApiError, useAuthStore } from '~/stores/auth'
import { useUiStore } from '~/stores/ui'

// /login：唯一免 JWT 页（doc/04 §2）。登录成功且 mustChangePassword 时进入首次改密步骤。
const authStore = useAuthStore()
const uiStore = useUiStore()

const mode = ref<'login' | 'changePassword'>('login')

onMounted(() => {
  uiStore.initTheme()
})

// 首次登录会就地切到改密步骤，标签页标题跟着走
useHead({ title: () => (mode.value === 'login' ? '登录' : '修改密码') })
const username = ref('')
const password = ref('')
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const errorMessage = ref('')
const noticeMessage = ref('')
const submitting = ref(false)

// 浏览器「建议使用强密码」/ 密码管理器自动填充是直接写 DOM 的 value，
// 不触发 input 事件 —— v-model 收不到，ref 仍是空串，于是出现「框里明明有内容
// 却报长度不足」。提交时统一以 DOM 实际值为准回填一次，两种输入方式都覆盖。
const usernameEl = ref<HTMLInputElement | null>(null)
const passwordEl = ref<HTMLInputElement | null>(null)
const newPasswordEl = ref<HTMLInputElement | null>(null)
const confirmPasswordEl = ref<HTMLInputElement | null>(null)

function syncFromDom(pairs: [HTMLInputElement | null, Ref<string>][]) {
  for (const [el, model] of pairs) {
    if (el && el.value !== model.value) model.value = el.value
  }
}

async function onLogin() {
  errorMessage.value = ''
  noticeMessage.value = ''
  syncFromDom([[usernameEl.value, username], [passwordEl.value, password]])
  if (!username.value.trim() || !password.value) {
    errorMessage.value = '请输入用户名与密码'
    return
  }
  submitting.value = true
  try {
    const user = await authStore.login(username.value.trim(), password.value)
    if (user.mustChangePassword) {
      // 首次登录强制改密（007 seed / admin 新建账号）
      mode.value = 'changePassword'
      oldPassword.value = password.value
      password.value = ''
      noticeMessage.value = '首次登录，请先修改密码'
    } else {
      await navigateTo('/')
    }
  } catch (err) {
    errorMessage.value = extractApiError(err, '登录失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

async function onChangePassword() {
  errorMessage.value = ''
  noticeMessage.value = ''
  syncFromDom([[newPasswordEl.value, newPassword], [confirmPasswordEl.value, confirmPassword]])
  if (newPassword.value.length < 8) {
    errorMessage.value = '新密码长度至少 8 位'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    errorMessage.value = '两次输入的新密码不一致'
    return
  }
  submitting.value = true
  try {
    await authStore.changePassword(oldPassword.value, newPassword.value)
    // 服务端已撤销全部登录状态，回到登录步骤
    mode.value = 'login'
    password.value = ''
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    noticeMessage.value = '密码已修改，请使用新密码重新登录'
  } catch (err) {
    errorMessage.value = extractApiError(err, '修改密码失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="container_login_auth_body_root">
    <button
      class="container_login_auth_header_theme"
      type="button"
      :title="uiStore.theme === 'light' ? '切换到夜间模式' : '切换到白天模式'"
      :aria-label="uiStore.theme === 'light' ? '切换到夜间模式' : '切换到白天模式'"
      @click="uiStore.toggleTheme()"
    >
      <span aria-hidden="true">{{ uiStore.theme === 'light' ? '☀️' : '🌙' }}</span>
      <span>{{ uiStore.theme === 'light' ? '白天' : '夜间' }}</span>
    </button>
    <form
      v-if="mode === 'login'"
      class="container_login_auth_form_root"
      @submit.prevent="onLogin"
    >
      <div class="container_login_auth_panel_hero">
        <span class="container_login_auth_hero_logo" aria-hidden="true">◆</span>
        <h1 class="container_login_auth_hero_title">COLAB 任务指挥台</h1>
        <p class="container_login_auth_hero_desc">
          云端算力调度中枢 · 实例托管与自愈
        </p>
      </div>

      <label class="container_login_auth_form_username">
        用户名
        <input ref="usernameEl" v-model="username" type="text" autocomplete="username" placeholder="请输入操作员账号" required>
      </label>
      <label class="container_login_auth_form_password">
        密码
        <input ref="passwordEl" v-model="password" type="password" autocomplete="current-password" placeholder="请输入访问密钥" required>
      </label>
      <p v-if="errorMessage" class="container_login_auth_form_error" data-state="error">
        {{ errorMessage }}
      </p>
      <p v-if="noticeMessage" class="container_login_auth_form_notice">{{ noticeMessage }}</p>
      <button class="container_login_auth_footer_submit" type="submit" :disabled="submitting">
        {{ submitting ? '验证中…' : '登入控制台' }}
      </button>
    </form>

    <form
      v-else
      class="container_login_auth_form_root"
      @submit.prevent="onChangePassword"
    >
      <div class="container_login_auth_panel_hero">
        <span class="container_login_auth_hero_logo" aria-hidden="true">◆</span>
        <h1 class="container_login_auth_hero_title">首次登录修改密码</h1>
        <p class="container_login_auth_hero_desc">
          安全规范要求：首次接入须更新访问密码
        </p>
      </div>

      <label class="container_login_auth_form_newpassword">
        新密码
        <input ref="newPasswordEl" v-model="newPassword" type="password" autocomplete="new-password" placeholder="不少于 8 位字符" required>
      </label>
      <label class="container_login_auth_form_confirmpassword">
        确认新密码
        <input ref="confirmPasswordEl" v-model="confirmPassword" type="password" autocomplete="new-password" placeholder="再次输入新密码" required>
      </label>
      <p v-if="errorMessage" class="container_login_auth_form_error" data-state="error">
        {{ errorMessage }}
      </p>
      <p v-if="noticeMessage" class="container_login_auth_form_notice">{{ noticeMessage }}</p>
      <button class="container_login_auth_footer_submit" type="submit" :disabled="submitting">
        {{ submitting ? '提交中…' : '更新密码并继续' }}
      </button>
    </form>
  </div>
</template>

<style>
.container_login_auth_body_root {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  min-height: 100dvh;
  padding: var(--sp-6) var(--sp-4);
}

.container_login_auth_form_root {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 100%;
  max-width: 400px;
  padding: var(--sp-8);
  margin: auto;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-top: 2px solid var(--c-brand);
  border-radius: var(--r-md);
  box-shadow: var(--sh-3);
}

.container_login_auth_panel_hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: var(--sp-2);
  text-align: center;
}

.container_login_auth_hero_logo {
  font-size: var(--fs-3xl);
  line-height: 1;
  color: var(--c-brand);
  text-shadow: var(--c-brand-glow);
}

.container_login_auth_hero_title {
  margin: var(--sp-2) 0 var(--sp-1);
  font-size: var(--fs-xl);
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--c-text);
}

.container_login_auth_hero_desc {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--c-text-sub);
}

.container_login_auth_form_username,
.container_login_auth_form_password,
.container_login_auth_form_newpassword,
.container_login_auth_form_confirmpassword {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

.container_login_auth_form_username input,
.container_login_auth_form_password input,
.container_login_auth_form_newpassword input,
.container_login_auth_form_confirmpassword input {
  padding: 10px 12px;
  font-size: var(--fs-md);
}

.container_login_auth_form_error {
  margin: 0;
  padding: 8px 12px;
  font-size: var(--fs-xs);
  color: var(--c-danger);
  background: var(--c-danger-bg);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
}

.container_login_auth_form_notice {
  margin: 0;
  padding: 8px 12px;
  font-size: var(--fs-xs);
  color: var(--c-success);
  background: var(--c-success-bg);
  border: 1px solid var(--c-success);
  border-radius: var(--r-sm);
}

.container_login_auth_footer_submit {
  padding: 12px 0;
  margin-top: var(--sp-2);
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast), transform var(--t-fast);
}

.container_login_auth_footer_submit:hover:not(:disabled) {
  filter: brightness(1.1);
  transform: translateY(-1px);
}

.container_login_auth_header_theme {
  position: fixed;
  top: var(--sp-4);
  right: var(--sp-4);
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 6px 12px;
  font-size: var(--fs-xs);
  color: var(--c-text);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
  box-shadow: var(--sh-1);
  transition:
    background-color var(--t-fast),
    border-color var(--t-fast),
    color var(--t-fast);
}

.container_login_auth_header_theme:hover {
  color: var(--c-brand);
  border-color: var(--c-brand);
}
</style>
