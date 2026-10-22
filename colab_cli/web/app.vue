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

const showNav = computed(() => route.path !== '/login' && authStore.isLoggedIn)
const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')
const isAdmin = computed(() => authStore.user?.role === 'admin')

// 会话轮询只在客户端、登录期间运行；登出即停（孤儿横幅数据来自轮询）
onMounted(() => {
  uiStore.initTheme()
  if (authStore.isLoggedIn) sessionStore.startPolling()
})
watch(() => authStore.isLoggedIn, (loggedIn) => {
  if (!import.meta.client) return
  if (loggedIn) sessionStore.startPolling()
  else sessionStore.stopPolling()
})
onBeforeUnmount(() => sessionStore.stopPolling())

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
    <header v-if="showNav" class="container_dash_list_header_nav">
      <div class="container_dash_list_header_brand">
        <span class="container_dash_list_header_logo" aria-hidden="true">◆</span>
        <span class="container_dash_list_header_title">COLAB 控制台</span>
      </div>
      <NuxtLink
        v-for="item in navItems"
        :key="item.to"
        class="container_dash_list_header_link"
        active-class="container_dash_list_header_current"
        :to="item.to"
      >
        {{ item.label }}
      </NuxtLink>
      <div class="container_dash_list_header_status">
        <span class="container_dash_list_header_dot" aria-hidden="true" />
        <span class="container_dash_list_header_state">系统在线</span>
      </div>
      <button
        class="container_dash_list_header_theme"
        type="button"
        :title="uiStore.theme === 'light' ? '切换到夜间模式' : '切换到白天模式'"
        :aria-label="uiStore.theme === 'light' ? '切换到夜间模式' : '切换到白天模式'"
        @click="uiStore.toggleTheme()"
      >
        <span aria-hidden="true">{{ uiStore.theme === 'light' ? '☀️' : '🌙' }}</span>
        <span>{{ uiStore.theme === 'light' ? '白天' : '夜间' }}</span>
      </button>
      <span class="container_dash_list_header_user">
        {{ authStore.user?.username }}
      </span>
      <button
        class="container_dash_list_header_logout"
        type="button"
        @click="authStore.logout()"
      >
        退出登录
      </button>
    </header>

    <SessionOrphanBanner v-if="showNav" />

    <NuxtPage
      :transition="{
        enterActiveClass: 'container_dash_list_body_entering',
        enterFromClass: 'container_dash_list_body_enterfrom',
        leaveActiveClass: 'container_dash_list_body_leaving',
        leaveToClass: 'container_dash_list_body_leaveto',
        mode: 'out-in',
      }"
    />

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
        role="status"
        @click="uiStore.remove(toast.id)"
      >
        {{ toast.text }}
      </div>
    </TransitionGroup>
  </div>
</template>

<style>
.container_dash_list_header_nav {
  display: flex;
  gap: var(--sp-1);
  align-items: center;
  padding: 0 var(--sp-6);
  background: var(--c-surface);
  border-bottom: 1px solid var(--c-border);
  box-shadow: var(--sh-1);
}

.container_dash_list_header_brand {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  margin-right: var(--sp-4);
  user-select: none;
}

.container_dash_list_header_logo {
  font-size: var(--fs-md);
  color: var(--c-brand);
  text-shadow: var(--c-brand-glow);
}

.container_dash_list_header_title {
  font-size: var(--fs-sm);
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--c-text);
  white-space: nowrap;
}

.container_dash_list_header_link {
  padding: var(--sp-3) 14px;
  color: var(--c-text-sub);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  transition:
    color var(--t-fast),
    border-color var(--t-fast);
}

.container_dash_list_header_link:hover {
  color: var(--c-brand);
}

.container_dash_list_header_current {
  font-weight: 600;
  color: var(--c-brand);
  border-bottom-color: var(--c-brand);
  text-shadow: var(--c-brand-glow-subtle);
}

.container_dash_list_header_status {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-left: auto;
  padding: 3px 10px;
  background: var(--c-success-bg-subtle);
  border: 1px solid var(--c-success-border);
  border-radius: 12px;
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

.container_dash_list_header_theme {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 4px 10px;
  margin-left: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
  transition:
    background-color var(--t-fast),
    border-color var(--t-fast),
    color var(--t-fast);
}

.container_dash_list_header_theme:hover {
  color: var(--c-brand);
  border-color: var(--c-brand);
}

.container_dash_list_header_user {
  margin-left: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--c-text-weak);
}

.container_dash_list_header_logout {
  margin-left: var(--sp-3);
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

/* 窄屏：导航项最多 7 个，做成可横向滑的 tab 条 */
@media (width <= 768px) {
  .container_dash_list_header_nav {
    padding: 0 var(--sp-2);
    overflow-x: auto;
    white-space: nowrap;
    -webkit-overflow-scrolling: touch;
  }

  .container_dash_list_header_brand {
    display: none;
  }

  .container_dash_list_header_link {
    flex: none;
    padding: var(--sp-3) var(--sp-2);
  }

  .container_dash_list_header_status,
  .container_dash_list_header_user {
    display: none;
  }

  .container_dash_list_header_theme {
    flex: none;
    margin-left: auto;
    padding: 3px 8px;
  }
}

.container_dash_list_modal_root {
  position: fixed;
  top: var(--sp-4);
  right: var(--sp-4);
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: 360px;
}

.container_dash_list_modal_toast {
  padding: 10px var(--sp-4);
  cursor: pointer;
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-left-width: 4px;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-2);
  color: var(--c-text);
  transition: filter var(--t-fast);
}

.container_dash_list_modal_toast:hover {
  filter: brightness(1.1);
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
