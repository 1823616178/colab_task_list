import { defineStore } from 'pinia'

export interface AuthUser {
  id: number
  username: string
  role: 'admin' | 'operator' | 'viewer'
  isActive: boolean
  mustChangePassword: boolean
  createdAt: string
  lastLoginAt: string | null
}

/** 服务端返回的错误体（doc/02 §3.4 统一格式）里取 message */
export function extractApiError(err: unknown, fallback: string): string {
  const data = (err as { data?: { error?: { message?: string } } })?.data
  return data?.error?.message ?? fallback
}

// doc/04 §3.1：auth 用户、accessToken、登录/刷新/登出
export const useAuthStore = defineStore('auth', () => {
  // accessToken 只放内存，不进 localStorage / Cookie
  const accessToken = ref<string | null>(null)
  const user = ref<AuthUser | null>(null)

  const isLoggedIn = computed(() => user.value !== null)

  function setAccessToken(token: string | null) {
    accessToken.value = token
  }

  function setSession(token: string, u: AuthUser) {
    accessToken.value = token
    user.value = u
  }

  function clear() {
    accessToken.value = null
    user.value = null
  }

  async function login(username: string, password: string): Promise<AuthUser> {
    const { $apiFetch } = useNuxtApp()
    const res = await $apiFetch<{ accessToken: string, user: AuthUser }>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
    })
    setSession(res.accessToken, res.user)
    return res.user
  }

  async function logout(): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    try {
      await $apiFetch('/api/auth/logout', { method: 'POST' })
    } catch {
      // 登出不因接口失败而中断：本地状态必须清掉
    }
    clear()
    await navigateTo('/login')
  }

  // 页面刷新后内存 token 丢失，借 refresh Cookie 恢复会话（401 → 自动刷新 → 重放）
  async function restore(): Promise<void> {
    if (user.value) return
    const { $apiFetch } = useNuxtApp()
    try {
      const res = await $apiFetch<{ user: AuthUser }>('/api/auth/me')
      user.value = res.user
    } catch {
      clear()
    }
  }

  // 首次登录强制改密（mustChangePassword）。成功后服务端撤销全部 refresh token，需重新登录
  async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch('/api/auth/change-password', {
      method: 'POST',
      body: { oldPassword, newPassword },
    })
    clear()
  }

  return {
    accessToken,
    user,
    isLoggedIn,
    setAccessToken,
    setSession,
    clear,
    login,
    logout,
    restore,
    changePassword,
  }
})
