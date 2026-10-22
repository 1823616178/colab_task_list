<script setup lang="ts">
// 全局骨架：导航（按 role 显隐，doc/04 §2）+ toast 容器（stores/ui.ts）
// + P4 孤儿 VM 全局告警横幅（doc/04 §3.3，SessionOrphanBanner，登录期间 30s 轮询驱动）。
// 浏览器标签页标题：各页面用 useHead({ title }) 只给自己那一段，
// 这里统一拼后缀。titleTemplate 写成函数（nuxt.config 里不支持函数，
// 只能在这里写），是为了让没设 title 的页面回落到纯站名而不是「 · 站名」。
const uiStore = useUiStore()

useHead({
  htmlAttrs: {
    lang: 'zh-CN',
    'data-theme': () => uiStore.theme,
  },
  script: [
    {
      innerHTML: `(function(){try{var t=localStorage.getItem('colab_theme')||(window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`,
    },
  ],
  titleTemplate: title => (title ? `${title} · Colab 控制台` : 'Colab 控制台'),
})

const route = useRoute()
const authStore = useAuthStore()
const sessionStore = useSessionStore()
const healthStore = useHealthStore()
const taskNotifyStore = useTaskNotifyStore()

async function onToggleNotify() {
  const permission = await taskNotifyStore.toggle()
  if (permission === 'unsupported') uiStore.warn('当前浏览器不支持桌面通知')
  else if (permission === 'denied') uiStore.warn('浏览器已拒绝通知权限，请在地址栏的网站设置中允许通知')
  else uiStore.info(taskNotifyStore.enabled ? '已开启任务完成桌面通知' : '已关闭任务完成桌面通知')
}

const showNav = computed(() => route.path !== '/login' && authStore.isLoggedIn)
const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')
const isAdmin = computed(() => authStore.user?.role === 'admin')

// 会话轮询只在客户端、登录期间运行；登出即停（孤儿横幅数据来自轮询）
onMounted(() => {
  uiStore.initTheme()
  if (authStore.isLoggedIn) {
    sessionStore.startPolling()
    healthStore.startPolling()
  }
  taskNotifyStore.init()
  if (authStore.isLoggedIn) taskNotifyStore.start()
})
watch(() => authStore.isLoggedIn, (loggedIn) => {
  if (!import.meta.client) return
  if (loggedIn) {
    sessionStore.startPolling()
    healthStore.startPolling()
    taskNotifyStore.start()
  } else {
    sessionStore.stopPolling()
    healthStore.stopPolling()
    taskNotifyStore.stop()
  }
})
onBeforeUnmount(() => {
  sessionStore.stopPolling()
  healthStore.stopPolling()
  taskNotifyStore.stop()
})

const navItems = computed(() => {
  const items: { to: string, label: string }[] = [
    { to: '/', label: '总览' },
    { to: '/tasks', label: '任务' },
    { to: '/history', label: '历史产物' },
  ]
  if (isOperator.value) {
    items.push(
      { to: '/tasks/new', label: '任务与工作流' },
      { to: '/sessions', label: '会话' },
    )
  }
  if (isAdmin.value) {
    items.push({ to: '/admin/users', label: '用户' })
  }
  return items
})
</script>

<template>
  <div>
    <a v-if="showNav" class="container_dash_list_header_skip" href="#main-content">跳到主要内容</a>
    <header v-if="showNav" class="container_dash_list_header_nav">
      <NuxtLink class="container_dash_list_header_brand" to="/" aria-label="Colab 控制台首页">
        <span class="container_dash_list_header_logo" aria-hidden="true">◆</span>
        <span class="container_dash_list_header_title">COLAB 控制台</span>
      </NuxtLink>
      <nav class="container_dash_list_header_menu" aria-label="主导航">
        <NuxtLink
          v-for="item in navItems"
          :key="item.to"
          class="container_dash_list_header_link"
          active-class="container_dash_list_header_current"
          :to="item.to"
        >
          {{ item.label }}
        </NuxtLink>
      </nav>
      <div class="container_dash_list_header_actions">
        <button
          class="container_dash_list_header_status"
          type="button"
          :data-health="healthStore.status"
          :title="healthStore.description"
          :aria-label="`系统状态：${healthStore.label}。${healthStore.description}`"
          @click="healthStore.refresh()"
        >
          <span class="container_dash_list_header_dot" aria-hidden="true" />
          <span class="container_dash_list_header_state">系统{{ healthStore.label }}</span>
        </button>
        <button
          class="container_dash_list_header_notify"
          type="button"
          :aria-pressed="taskNotifyStore.enabled"
          :title="taskNotifyStore.enabled ? '任务完成桌面通知：已开启（点击关闭）' : '任务完成桌面通知：已关闭（点击开启）'"
          :aria-label="taskNotifyStore.enabled ? '关闭任务完成桌面通知' : '开启任务完成桌面通知'"
          @click="onToggleNotify"
        >
          <span aria-hidden="true">{{ taskNotifyStore.enabled ? '🔔' : '🔕' }}</span>
        </button>
        <button
          class="container_dash_list_header_theme"
          type="button"
          :title="uiStore.theme === 'light' ? '切换到夜间模式' : '切换到白天模式'"
          :aria-label="uiStore.theme === 'light' ? '切换到夜间模式' : '切换到白天模式'"
          @click="uiStore.toggleTheme()"
        >
          <span aria-hidden="true">{{ uiStore.theme === 'light' ? '☀️' : '🌙' }}</span>
          <span class="container_dash_list_header_themetext">{{ uiStore.theme === 'light' ? '白天' : '夜间' }}</span>
        </button>
        <span class="container_dash_list_header_user" :title="`当前用户：${authStore.user?.username ?? ''}`">
          <span class="container_dash_list_header_avatar" aria-hidden="true">
            {{ (authStore.user?.username ?? '?').slice(0, 1).toUpperCase() }}
          </span>
          <span class="container_dash_list_header_username">{{ authStore.user?.username }}</span>
        </span>
        <button
          class="container_dash_list_header_logout"
          type="button"
          @click="authStore.logout()"
        >
          退出登录
        </button>
      </div>
    </header>

    <SessionOrphanBanner v-if="showNav" />

    <div id="main-content" class="container_dash_list_body_main" tabindex="-1">
      <NuxtPage
        :transition="{
          enterActiveClass: 'container_dash_list_body_entering',
          enterFromClass: 'container_dash_list_body_enterfrom',
          leaveActiveClass: 'container_dash_list_body_leaving',
          leaveToClass: 'container_dash_list_body_leaveto',
          mode: 'out-in',
        }"
      />
    </div>

    <!-- TransitionGroup 的过渡类名必须显式指定：Vue 默认生成的 toast-enter-active
         之类不符合 container_页面_功能_位置_别名（doc/04 §1.1），stylelint 会拦。
         tag="div" 让它直接充当原来的容器，不额外增加一层 DOM。 -->
    <TransitionGroup
      tag="div"
      class="container_dash_list_modal_root"
      aria-live="polite"
      enter-active-class="container_dash_list_modal_entering"
      enter-from-class="container_dash_list_modal_enterfrom"
      leave-active-class="container_dash_list_modal_leaving"
      leave-to-class="container_dash_list_modal_leaveto"
      move-class="container_dash_list_modal_moving"
    >
      <div
        v-for="toast in uiStore.toasts"
        :key="toast.id"
        class="container_dash_list_modal_toast"
        :data-kind="toast.kind"
        :role="toast.kind === 'error' ? 'alert' : 'status'"
      >
        <span class="container_dash_list_modal_icon" aria-hidden="true">
          {{ toast.kind === 'error' ? '✕' : toast.kind === 'warn' ? '!' : 'i' }}
        </span>
        <span class="container_dash_list_modal_text">{{ toast.text }}</span>
        <button
          class="container_dash_list_modal_close"
          type="button"
          aria-label="关闭提示"
          @click="uiStore.remove(toast.id)"
        >
          ×
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style>
.container_dash_list_header_skip {
  position: absolute;
  top: var(--sp-2);
  left: var(--sp-2);
  z-index: calc(var(--z-toast) + 1);
  padding: var(--sp-2) var(--sp-4);
  color: var(--c-text-invert);
  background: var(--c-brand);
  border-radius: var(--r-sm);
  transform: translateY(-200%);
}

.container_dash_list_header_skip:focus {
  color: var(--c-text-invert);
  transform: none;
}

.container_dash_list_body_main:focus {
  outline: none;
}

.container_dash_list_header_nav {
  position: sticky;
  top: 0;
  z-index: var(--z-header);
  display: flex;
  gap: var(--sp-4);
  align-items: center;
  min-height: var(--h-header);
  padding: 0 var(--sp-6);
  background: color-mix(in srgb, var(--c-surface) 88%, transparent);
  border-bottom: 1px solid var(--c-border);
  box-shadow: var(--sh-1);
  backdrop-filter: saturate(160%) blur(10px);
}

.container_dash_list_header_brand {
  display: flex;
  flex: none;
  gap: var(--sp-2);
  align-items: center;
  color: var(--c-text);
  user-select: none;
}

.container_dash_list_header_brand:hover {
  color: var(--c-text);
  text-shadow: none;
}

.container_dash_list_header_logo {
  font-size: var(--fs-lg);
  color: var(--c-brand);
  text-shadow: var(--c-brand-glow);
}

.container_dash_list_header_title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-bold);
  letter-spacing: 1px;
  white-space: nowrap;
}

.container_dash_list_header_menu {
  display: flex;
  flex: 1;
  gap: var(--sp-1);
  align-self: stretch;
  min-width: 0;
}

.container_dash_list_header_link {
  display: flex;
  align-items: center;
  padding: 0 var(--sp-3);
  color: var(--c-text-sub);
  white-space: nowrap;
  text-decoration: none;
  border-bottom: 2px solid transparent;
  transition:
    color var(--t-fast),
    border-color var(--t-fast),
    background-color var(--t-fast);
}

.container_dash_list_header_link:hover {
  color: var(--c-text);
  background: var(--c-surface-hover);
  text-shadow: none;
}

.container_dash_list_header_current,
.container_dash_list_header_current:hover {
  font-weight: var(--fw-semibold);
  color: var(--c-brand);
  border-bottom-color: var(--c-brand);
}

.container_dash_list_header_actions {
  display: flex;
  flex: none;
  gap: var(--sp-2);
  align-items: center;
}

.container_dash_list_header_status {
  display: flex;
  gap: 6px;
  align-items: center;
  min-height: 0;
  padding: 3px 10px;
  background: var(--c-success-bg-subtle);
  border: 1px solid var(--c-success-border);
  border-radius: var(--r-pill);
}

.container_dash_list_header_dot {
  width: 6px;
  height: 6px;
  background: var(--c-success);
  border-radius: 50%;
  box-shadow: var(--c-success-glow);
}

.container_dash_list_header_state {
  font-size: var(--fs-xs);
  color: var(--c-success);
}

.container_dash_list_header_status:hover:not(:disabled) {
  filter: none;
}

.container_dash_list_header_status[data-health='degraded'] {
  background: var(--c-warn-bg);
  border-color: var(--c-warn-border);
}

.container_dash_list_header_status[data-health='degraded'] .container_dash_list_header_dot {
  background: var(--c-warn);
  box-shadow: none;
}

.container_dash_list_header_status[data-health='degraded'] .container_dash_list_header_state {
  color: var(--c-warn-text);
}

.container_dash_list_header_status[data-health='offline'] {
  background: var(--c-danger-bg);
  border-color: var(--c-danger);
}

.container_dash_list_header_status[data-health='offline'] .container_dash_list_header_dot {
  background: var(--c-danger);
  box-shadow: none;
}

.container_dash_list_header_status[data-health='offline'] .container_dash_list_header_state {
  color: var(--c-danger);
}

.container_dash_list_header_status[data-health='unknown'] .container_dash_list_header_dot {
  background: var(--c-text-weak);
  box-shadow: none;
}

.container_dash_list_header_status[data-health='unknown'] .container_dash_list_header_state {
  color: var(--c-text-weak);
}

.container_dash_list_header_notify {
  padding: 4px 8px;
  font-size: var(--fs-xs);
}

.container_dash_list_header_notify[aria-pressed='true'] {
  border-color: var(--c-brand-border);
  background: var(--c-brand-bg);
}

.container_dash_list_header_theme {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 4px 10px;
  font-size: var(--fs-xs);
}

.container_dash_list_header_theme:hover:not(:disabled) {
  color: var(--c-brand);
  border-color: var(--c-brand);
}

.container_dash_list_header_user {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

.container_dash_list_header_avatar {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 24px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-bold);
  color: var(--c-brand);
  background: var(--c-brand-bg);
  border: 1px solid var(--c-brand-border);
  border-radius: 50%;
}

.container_dash_list_header_logout {
  padding: 4px 10px;
  font-size: var(--fs-xs);
}

/* 页面切换动效（8–12px 位移 + 淡入，180–260ms，doc/09 §4） */
.container_dash_list_body_entering,
.container_dash_list_body_leaving {
  transition:
    opacity 200ms ease,
    transform 200ms ease;
}

.container_dash_list_body_enterfrom {
  opacity: 0;
  transform: translateY(10px);
}

.container_dash_list_body_leaveto {
  opacity: 0;
  transform: translateY(-8px);
}

/* 中屏：收起次要信息 */
@media (width <= 1100px) {
  .container_dash_list_header_status,
  .container_dash_list_header_username {
    display: none;
  }
}

/* 窄屏：品牌 + 操作占第一行，导航变成第二行可横向滑动的 tab 条 */
@media (width <= 768px) {
  .container_dash_list_header_nav {
    flex-wrap: wrap;
    row-gap: 0;
    padding: var(--sp-2) var(--sp-3) 0;
  }

  .container_dash_list_header_actions {
    margin-left: auto;
  }

  .container_dash_list_header_themetext {
    display: none;
  }

  .container_dash_list_header_menu {
    flex-basis: 100%;
    order: 3;
    min-height: 40px;
    margin: var(--sp-1) calc(var(--sp-3) * -1) 0;
    padding: 0 var(--sp-2);
    overflow-x: auto;
    scrollbar-width: none;
    border-top: 1px solid var(--c-divider);
    -webkit-overflow-scrolling: touch;
  }

  .container_dash_list_header_link {
    flex: none;
    padding: 0 var(--sp-3);
  }
}

.container_dash_list_modal_root {
  position: fixed;
  top: calc(var(--h-header) + var(--sp-3));
  right: var(--sp-4);
  z-index: var(--z-toast);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(380px, calc(100vw - var(--sp-8)));
}

.container_dash_list_modal_toast {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
  padding: 10px var(--sp-3);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-left-width: 4px;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-2);
  color: var(--c-text);
  transition: filter var(--t-fast);
}

.container_dash_list_modal_icon {
  display: inline-grid;
  flex: none;
  place-items: center;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-bold);
  color: var(--c-text-invert);
  background: var(--c-brand);
  border-radius: 50%;
}

.container_dash_list_modal_toast[data-kind='warn'] .container_dash_list_modal_icon {
  background: var(--c-warn);
}

.container_dash_list_modal_toast[data-kind='error'] .container_dash_list_modal_icon {
  background: var(--c-danger);
}

.container_dash_list_modal_text {
  flex: 1;
  overflow-wrap: anywhere;
}

.container_dash_list_modal_close {
  flex: none;
  min-height: 0;
  padding: 0 6px;
  font-size: var(--fs-lg);
  line-height: 1.2;
  color: var(--c-text-weak);
  background: transparent;
  border: none;
}

.container_dash_list_modal_close:hover:not(:disabled) {
  color: var(--c-text);
}

.container_dash_list_modal_toast[data-kind='info'] {
  border-left-color: var(--c-brand);
  box-shadow: var(--sh-toast-info);
}

.container_dash_list_modal_toast[data-kind='warn'] {
  border-left-color: var(--c-warn);
  box-shadow: var(--sh-toast-warn);
}

.container_dash_list_modal_toast[data-kind='error'] {
  border-left-color: var(--c-danger);
  box-shadow: var(--sh-toast-error);
}

.container_dash_list_modal_entering,
.container_dash_list_modal_leaving,
.container_dash_list_modal_moving {
  transition:
    opacity var(--t-base),
    transform var(--t-base);
}

/* 从右侧滑入 —— toast 停在右上角，从它来的方向进场 */
.container_dash_list_modal_enterfrom,
.container_dash_list_modal_leaveto {
  opacity: 0;
  transform: translateX(16px);
}

/* 离场的那条要脱离文档流，否则下面的 toast 会等它收完才上移 */
.container_dash_list_modal_leaving {
  position: absolute;
}
</style>
