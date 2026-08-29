// doc/04 §3：鉴权路由中间件。/login 是唯一免 JWT 页。
export default defineNuxtRouteMiddleware(async (to) => {
  // SSR 不是重点（doc/01 §3），鉴权只在客户端做；refresh Cookie 由浏览器自带
  if (import.meta.server) return
  const authStore = useAuthStore()
  if (!authStore.user) await authStore.restore()
  if (!authStore.user && to.path !== '/login') return navigateTo('/login')
  // mustChangePassword 的用户允许停在 /login 完成首次改密
  if (authStore.user && to.path === '/login' && !authStore.user.mustChangePassword) {
    return navigateTo('/')
  }
})
