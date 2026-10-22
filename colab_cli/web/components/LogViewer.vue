<script setup lang="ts">
// 任务详情页 · 实时日志（doc/04 §2.2）：
// 手写虚拟滚动只渲染可视行（长任务几万行）；「跟随底部」开关，用户上滚自动关闭；
// \r 刷新行的就地覆盖在 taskStream store 完成，这里只负责渲染。
const props = defineProps<{ lines: string[] }>()

const ROW_HEIGHT = 20
const VIEWPORT_HEIGHT = 480
const OVERSCAN = 10
/** 距底不足两行视为「在底部」，恢复跟随 */
const BOTTOM_THRESHOLD = ROW_HEIGHT * 2

const viewport = ref<HTMLElement | null>(null)
const scrollTop = ref(0)
const followBottom = ref(true)

const startIndex = computed(() =>
  Math.max(0, Math.floor(scrollTop.value / ROW_HEIGHT) - OVERSCAN))
const endIndex = computed(() =>
  Math.min(props.lines.length,
    Math.ceil((scrollTop.value + VIEWPORT_HEIGHT) / ROW_HEIGHT) + OVERSCAN))

const visibleRows = computed(() => {
  const rows: { index: number, text: string }[] = []
  for (let i = startIndex.value; i < endIndex.value; i++) {
    rows.push({ index: i, text: props.lines[i] ?? '' })
  }
  return rows
})

const totalHeight = computed(() => props.lines.length * ROW_HEIGHT)

function onScroll() {
  const el = viewport.value
  if (!el) return
  scrollTop.value = el.scrollTop
  // 用户上滚 → 自动关闭跟随；回到底部 → 重新开启
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD
}

async function scrollToBottom() {
  await nextTick()
  viewport.value?.scrollTo({ top: viewport.value.scrollHeight })
}

watch(() => props.lines.length, () => {
  if (followBottom.value) void scrollToBottom()
})

function toggleFollow() {
  followBottom.value = !followBottom.value
  if (followBottom.value) void scrollToBottom()
}
</script>

<template>
  <section class="container_task_detail_panel_log">
    <div class="container_task_detail_toolbar_log">
      <h2 class="container_task_detail_header_sectiontitle">实时日志</h2>
      <span class="container_task_detail_toolbar_linecount">{{ lines.length }} 行</span>
      <button
        class="container_task_detail_toolbar_follow"
        type="button"
        :data-on="followBottom ? 'true' : 'false'"
        :aria-pressed="followBottom"
        @click="toggleFollow"
      >
        {{ followBottom ? '跟随底部：开' : '跟随底部：关' }}
      </button>
    </div>
    <div
      ref="viewport"
      class="container_task_detail_panel_logscroll"
      :style="{ height: `${VIEWPORT_HEIGHT}px` }"
      @scroll.passive="onScroll"
    >
      <div class="container_task_detail_panel_logspace" :style="{ height: `${totalHeight}px` }">
        <div
          v-for="row in visibleRows"
          :key="row.index"
          class="container_task_detail_panel_logline"
          :style="{ transform: `translateY(${row.index * ROW_HEIGHT}px)` }"
        >
          <span class="container_task_detail_panel_lineno">{{ row.index + 1 }}</span>
          <span class="container_task_detail_panel_linetext">{{ row.text }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style>
.container_task_detail_panel_log {
  padding: var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_detail_toolbar_log {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin-bottom: var(--sp-3);
}

.container_task_detail_toolbar_log .container_task_detail_header_sectiontitle {
  margin: 0;
}

.container_task_detail_toolbar_linecount {
  color: var(--c-text-weak);
  font-size: var(--fs-xs);
}

.container_task_detail_toolbar_follow {
  padding: var(--sp-1) var(--sp-3);
  margin-left: auto;
}

.container_task_detail_toolbar_follow[data-on='true'] {
  color: var(--c-text-invert);
  background: var(--c-brand);
  border-color: var(--c-brand);
}

.container_task_detail_panel_logscroll {
  overflow-y: auto;
  background: var(--c-log-bg);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_task_detail_panel_logspace {
  position: relative;
}

.container_task_detail_panel_logline {
  position: absolute;
  top: 0;
  left: 0;
  display: flex;
  width: 100%;
  height: 20px;
  padding-right: var(--sp-3);
  overflow: hidden;
  font-family: var(--ff-mono);
  font-size: var(--fs-xs);
  line-height: 20px;
  color: var(--c-log-text);
  white-space: pre;
  transition: background-color var(--t-fast);
}

.container_task_detail_panel_logline:hover {
  background: var(--c-brand-bg);
}

.container_task_detail_panel_lineno {
  flex: none;
  width: 56px;
  padding-right: var(--sp-3);
  color: var(--c-log-lineno);
  text-align: right;
  user-select: none;
}
</style>
