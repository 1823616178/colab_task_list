import type { NitroFetchOptions } from 'nitropack'

// 选项类型跟着 $fetch 走（NitroFetchOptions 而不是 ofetch 的 FetchOptions）：
// 后者的 method 是宽泛 string、retryDelay 绑死 'json' 响应类型，与 $fetch 对不上。
export type ApiFetch = <T = unknown>(
  request: string,
  options?: NitroFetchOptions<string>,
) => Promise<T>

// doc/04 §3.1：$apiFetch 遇 401 → 自动打 /api/auth/refresh → 重放原请求；
// 并发 401 合流成一次刷新（shared promise），否则会触发后端「token 重用 → 撤销全部」保护；
// 刷新失败 → 清 store 跳 /login。
let refreshPromise: Promise<string | null> | null = null

export default defineNuxtPlugin(() => {
  const authStore = useAuthStore()

  function refreshOnce(): Promise<string | null> {
    if (!refreshPromise) {
      refreshPromise = $fetch<{ accessToken: string }>('/api/auth/refresh', { method: 'POST' })
        .then((res) => {
          authStore.setAccessToken(res.accessToken)
          return res.accessToken
        })
        .catch(() => {
          authStore.clear()
          return null
        })
        .finally(() => {
          refreshPromise = null
        })
    }
    return refreshPromise
  }

  const apiFetch: ApiFetch = async (request, options = {}) => {
    const send = () =>
      $fetch(request, {
        ...options,
        headers: {
          ...(options.headers as Record<string, string> | undefined),
          ...(authStore.accessToken ? { Authorization: `Bearer ${authStore.accessToken}` } : {}),
        },
      })
    try {
      // 这里必须显式断言：$fetch 的泛型经闭包包装后无法自动传导
      return await send() as never
    } catch (err) {
      const status = (err as { statusCode?: number, response?: { status?: number } })
        ?.statusCode ?? (err as { response?: { status?: number } })?.response?.status
      const isAuthEndpoint = request.startsWith('/api/auth/login') || request.startsWith('/api/auth/refresh')
      if (status === 401 && !isAuthEndpoint) {
        const token = await refreshOnce()
        if (!token) {
          authStore.clear()
          await navigateTo('/login')
          throw err
        }
        return await send() as never // 用新 token 重放原请求（仅一次）
      }
      throw err
    }
  }

  // $apiRefresh：给非 $fetch 场景（fetch 流式 SSE、blob 下载）复用同一条合流刷新通道，
  // 避免并发刷新触发后端「token 重用 → 撤销全部」保护
  return { provide: { apiFetch, apiRefresh: refreshOnce } }
})

declare module '#app' {
  interface NuxtApp {
    $apiFetch: ApiFetch
    $apiRefresh: () => Promise<string | null>
  }
}

declare module 'vue' {
  interface ComponentCustomProperties {
    $apiFetch: ApiFetch
    $apiRefresh: () => Promise<string | null>
  }
}
