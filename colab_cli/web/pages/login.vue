<script setup lang="ts">
import type { Ref } from 'vue'
import { extractApiError, useAuthStore } from '~/stores/auth'

// /login：唯一免 JWT 页（doc/04 §2）。登录成功且 mustChangePassword 时进入首次改密步骤。
const authStore = useAuthStore()

const mode = ref<'login' | 'changePassword'>('login')

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
    <form
      v-if="mode === 'login'"
      class="container_login_auth_form_root"
      @submit.prevent="onLogin"
    >
      <h1 class="container_login_auth_header_title">Colab 控制台</h1>
      <label class="container_login_auth_form_username">
        用户名
        <input ref="usernameEl" v-model="username" type="text" autocomplete="username" required>
      </label>
      <label class="container_login_auth_form_password">
        密码
        <input ref="passwordEl" v-model="password" type="password" autocomplete="current-password" required>
      </label>
      <p v-if="errorMessage" class="container_login_auth_form_error" data-state="error">
        {{ errorMessage }}
      </p>
      <p v-if="noticeMessage" class="container_login_auth_form_notice">{{ noticeMessage }}</p>
      <button class="container_login_auth_footer_submit" type="submit" :disabled="submitting">
        {{ submitting ? '登录中…' : '登录' }}
      </button>
    </form>

    <form
      v-else
      class="container_login_auth_form_root"
      @submit.prevent="onChangePassword"
    >
      <h1 class="container_login_auth_header_title">修改密码</h1>
      <label class="container_login_auth_form_newpassword">
        新密码
        <input ref="newPasswordEl" v-model="newPassword" type="password" autocomplete="new-password" required>
      </label>
      <label class="container_login_auth_form_confirmpassword">
        确认新密码
        <input ref="confirmPasswordEl" v-model="confirmPassword" type="password" autocomplete="new-password" required>
      </label>
      <p v-if="errorMessage" class="container_login_auth_form_error" data-state="error">
        {{ errorMessage }}
      </p>
      <p v-if="noticeMessage" class="container_login_auth_form_notice">{{ noticeMessage }}</p>
      <button class="container_login_auth_footer_submit" type="submit" :disabled="submitting">
        {{ submitting ? '提交中…' : '修改密码' }}
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
}

.container_login_auth_form_root {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 360px;
  padding: var(--sp-8);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);

  /* 登录页是整个控制台的门面，也是唯一一张浮在空背景上的卡片，
     用最重的一档投影把它托起来（其余卡片一律 --sh-1）。 */
  box-shadow: var(--sh-3);
}


.container_login_auth_header_title {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-2xl);
  font-weight: 600;
  letter-spacing: 0.5px;
  text-align: center;
}

.container_login_auth_form_username,
.container_login_auth_form_password,
.container_login_auth_form_newpassword,
.container_login_auth_form_confirmpassword {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.container_login_auth_form_username input,
.container_login_auth_form_password input,
.container_login_auth_form_newpassword input,
.container_login_auth_form_confirmpassword input {
  padding: var(--sp-2) 10px;
}

.container_login_auth_form_error {
  margin: 0;
  color: var(--c-danger);
}

.container_login_auth_form_notice {
  margin: 0;
  color: var(--c-success);
}

.container_login_auth_footer_submit {
  padding: 10px 0;
  font-weight: 500;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
}

</style>
