<script setup lang="ts">
// 孤儿 VM 全局告警横幅（doc/04 §3.3）：在 app.vue 层常驻，
// 不限于 /sessions 页 —— 孤儿 VM 在持续烧计算单元，用户在哪个页面都得看见。
const sessionStore = useSessionStore()
const count = computed(() => sessionStore.orphans.length)
</script>

<template>
  <div v-if="count > 0" class="container_session_reconcile_panel_orphan" role="alert">
    <span class="container_session_reconcile_panel_text">
      ⚠️ 检测到 {{ count }} 个孤儿会话：VM 仍在计费但无人认领
      （{{ sessionStore.orphans.map(o => o.name).join('、') }}）
    </span>
    <NuxtLink class="container_session_reconcile_panel_action" to="/sessions">
      去处理
    </NuxtLink>
  </div>
</template>

<style>
.container_session_reconcile_panel_orphan {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  justify-content: center;
  padding: 10px var(--sp-4);
  color: var(--c-text-invert);
  background: var(--c-danger);
}

.container_session_reconcile_panel_text {
  font-size: var(--fs-md);
  font-weight: 600;
}

/* 红底横幅里的白色药丸按钮。必须显式写 :hover —— 否则 main.css 的
   `a:hover` 会把它改成品牌蓝，在红底上既刺眼又丢了「危险」语义。 */
.container_session_reconcile_panel_action {
  padding: var(--sp-1) 14px;
  font-weight: 600;
  color: var(--c-danger);
  text-decoration: none;
  background: var(--c-surface);
  border-radius: var(--r-sm);
  transition: background-color var(--t-fast);
}

.container_session_reconcile_panel_action:hover {
  color: var(--c-danger-hover);
  background: var(--c-danger-bg);
}
</style>
