<script setup lang="ts">
// 兼容旧工作流地址，统一进入任务与工作流页面。
await navigateTo('/tasks/new?tab=workflows', { replace: true })
</script>

<template>
  <div />
</template>
