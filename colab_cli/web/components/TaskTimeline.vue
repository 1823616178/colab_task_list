<script setup lang="ts">
import type { PhaseNode } from '~/stores/taskStream'

// 任务详情页 · 阶段时间线（doc/04 §2.2）：
// 数据源为 task_events 历史回放 + SSE start/ok/warn/error，低频刷新。
defineProps<{ phases: PhaseNode[] }>()
</script>

<template>
  <section class="container_task_detail_timeline_phase">
    <h2 class="container_task_detail_header_sectiontitle">阶段时间线</h2>
    <p v-if="phases.length === 0" class="container_task_detail_timeline_empty">暂无阶段事件</p>
    <ol v-else class="container_task_detail_timeline_list">
      <li
        v-for="node in phases"
        :key="`${node.phase}-${node.seq}`"
        class="container_task_detail_timeline_row"
        :data-status="node.status"
      >
        <span class="container_task_detail_timeline_dot" aria-hidden="true" />
        <span class="container_task_detail_timeline_name">{{ formatPhase(node.phase) }}</span>
        <span class="container_task_detail_timeline_message" :title="node.message">{{ node.message }}</span>
        <span class="container_task_detail_timeline_time">
          {{ formatLocalTime(node.startTs) }} · {{ formatDurationBetween(node.startTs, node.endTs) }}
        </span>
      </li>
    </ol>
  </section>
</template>

<style>
.container_task_detail_timeline_phase {
  padding: var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_detail_header_sectiontitle {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-lg);
}

.container_task_detail_timeline_empty {
  margin: 0;
  color: var(--c-text-weak);
}

.container_task_detail_timeline_list {
  padding: 0;
  margin: 0;
  list-style: none;
}

.container_task_detail_timeline_row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  padding: 6px 0;
  border-bottom: 1px dashed var(--c-divider);
}

.container_task_detail_timeline_row:last-child {
  border-bottom: none;
}

.container_task_detail_timeline_dot {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--c-border-strong);
}

/* 只有「正在跑」的阶段点会呼吸 —— 全站唯一的循环动画，
   用来区分「这一步还在动」和「这一步停在这儿了」。 */
.container_task_detail_timeline_row[data-status='active'] .container_task_detail_timeline_dot {
  background: var(--c-running);
  animation: pulse-dot 1.6s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.35;
  }
}

.container_task_detail_timeline_row[data-status='ok'] .container_task_detail_timeline_dot {
  background: var(--c-success);
}

.container_task_detail_timeline_row[data-status='warn'] .container_task_detail_timeline_dot {
  background: var(--c-warn);
}

.container_task_detail_timeline_row[data-status='error'] .container_task_detail_timeline_dot {
  background: var(--c-danger);
}

.container_task_detail_timeline_name {
  flex: none;
  min-width: 96px;
  font-weight: 500;
}

.container_task_detail_timeline_message {
  flex: 1;
  overflow: hidden;
  color: var(--c-text-sub);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.container_task_detail_timeline_time {
  flex: none;
  color: var(--c-text-weak);
  font-size: var(--fs-xs);
}
</style>
