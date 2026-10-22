<script setup lang="ts">
// 孤儿 VM 全局告警横幅（doc/04 §3.3）：在 app.vue 层常驻，
// 不限于 /sessions 页 —— 孤儿 VM 在持续烧计算单元，用户在哪个页面都得看见。
const sessionStore = useSessionStore()
const count = computed(() => sessionStore.orphans.length)
</script>

<template>
  <div v-if="count > 0" class="container_session_reconcile_panel_orphan" role="alert">
    <span class="container_session_reconcile_panel_icon" aria-hidden="true">!</span>
    <span class="container_session_reconcile_panel_text">
      <strong>检测到 {{ count }} 个孤儿会话：</strong>VM 仍在计费但无人认领
      <span class="container_session_reconcile_panel_names">
        （{{ sessionStore.orphans.map(o => o.name).join('、') }}）
      </span>
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
  max-width: var(--w-page);
  margin: var(--sp-4) auto 0;
  padding: 10px var(--sp-4);
  color: var(--c-text);
  background: var(--c-danger-bg);
  border: 1px solid var(--c-danger);
  border-left-width: 4px;
  border-radius: var(--r-md);
  box-shadow: var(--sh-glow-danger);
}

.container_session_reconcile_panel_icon {
  display: inline-grid;
  flex: none;
  place-items: center;
  width: 22px;
  height: 22px;
  font-weight: var(--fw-bold);
  color: var(--c-text-invert);
  background: var(--c-danger);
  border-radius: 50%;
}

.container_session_reconcile_panel_text {
  flex: 1;
  font-size: var(--fs-md);
}

.container_session_reconcile_panel_text strong {
  color: var(--c-danger);
}

.container_session_reconcile_panel_names {
  font-family: var(--ff-mono);
  font-size: var(--fs-sm);
  color: var(--c-text-sub);
}

/* 危险横幅里的处理按钮 */
.container_session_reconcile_panel_action {
  flex: none;
  padding: var(--sp-1) 14px;
  font-weight: var(--fw-semibold);
  color: var(--c-text-invert);
  text-decoration: none;
  background: var(--c-danger);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
  transition: background-color var(--t-fast);
}

.container_session_reconcile_panel_action:hover {
  color: var(--c-text-invert);
  background: var(--c-danger-hover);
  text-shadow: none;
}

@media (width <= 1392px) {
  .container_session_reconcile_panel_orphan {
    margin-right: var(--sp-4);
    margin-left: var(--sp-4);
  }
}
</style>
