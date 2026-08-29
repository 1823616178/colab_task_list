// 带 JWT 的浏览器下载：/api/** 需要 Authorization 头（server/middleware/auth.ts），
// <a href> 直连会 401，因此用 fetch 拉 blob 再触发下载。
// 401 时复用 apiFetch 插件的合流刷新（$apiRefresh）。

/** 下载 /api 下的文件（如 /api/tasks/:id/output、/api/tasks/:id/log）。失败抛错由调用方提示。 */
export async function downloadApiFile(url: string, filename: string): Promise<void> {
  const authStore = useAuthStore()
  const { $apiRefresh } = useNuxtApp()

  const send = (token: string | null) =>
    fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })

  let res = await send(authStore.accessToken)
  if (res.status === 401) {
    const token = await $apiRefresh()
    if (!token) {
      await navigateTo('/login')
      throw new Error('登录已过期')
    }
    res = await send(token)
  }
  if (!res.ok) {
    let message = `下载失败（HTTP ${res.status}）`
    try {
      const body = await res.json() as { error?: { message?: string } }
      if (body.error?.message) message = body.error.message
    } catch { /* 非 JSON 错误体，用默认文案 */ }
    throw new Error(message)
  }
  const blob = await res.blob()
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(objectUrl)
}
