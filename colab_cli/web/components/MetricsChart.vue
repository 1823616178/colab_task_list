<script setup lang="ts">
import type { MetricSample } from '~/stores/taskStream'

// 任务详情页 · 资源图表（doc/04 §2.2）：SSE metrics.sample，2 秒一个点。
// 手绘 SVG 折线/面积图（gpu_util 与 vram 占用百分比），不引图表库。
const props = defineProps<{ samples: MetricSample[] }>()

const WIDTH = 640
const HEIGHT = 160
const PAD_LEFT = 36
const PAD_RIGHT = 8
const PAD_TOP = 8
const PAD_BOTTOM = 20

const plotWidth = WIDTH - PAD_LEFT - PAD_RIGHT
const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM

function toPoints(values: number[]): string {
  const n = values.length
  if (n === 0) return ''
  return values
    .map((v, i) => {
      const x = PAD_LEFT + (n === 1 ? plotWidth : (i / (n - 1)) * plotWidth)
      const y = PAD_TOP + plotHeight - (Math.min(100, Math.max(0, v)) / 100) * plotHeight
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

const gpuPoints = computed(() => toPoints(props.samples.map(s => s.gpuUtil)))
const vramPoints = computed(() =>
  toPoints(props.samples.map(s => (s.vramTotalMb > 0 ? (s.vramUsedMb / s.vramTotalMb) * 100 : 0))),
)

const gpuArea = computed(() => {
  if (props.samples.length === 0) return ''
  const baseY = PAD_TOP + plotHeight
  return `${PAD_LEFT},${baseY} ${gpuPoints.value} ${PAD_LEFT + plotWidth},${baseY}`
})

const latest = computed(() => props.samples[props.samples.length - 1] ?? null)

const startTime = computed(() => props.samples[0]?.ts ?? null)
const endTime = computed(() => latest.value?.ts ?? null)

function shortTime(value: string | null): string {
  return value ? new Date(value).toLocaleTimeString() : ''
}
</script>

<template>
  <section class="container_task_detail_panel_metrics">
    <h2 class="container_task_detail_header_sectiontitle">资源指标</h2>
    <p v-if="samples.length === 0" class="container_task_detail_metrics_empty">
      暂无指标数据（metrics.sample 仅实时推送，终态任务不保留）
    </p>
    <template v-else>
      <svg
        class="container_task_detail_metrics_chart"
        :viewBox="`0 0 ${WIDTH} ${HEIGHT}`"
        role="img"
        aria-label="GPU 利用率与显存占用曲线"
      >
        <line
          v-for="tick in [0, 50, 100]"
          :key="tick"
          class="container_task_detail_metrics_grid"
          :x1="PAD_LEFT"
          :x2="WIDTH - PAD_RIGHT"
          :y1="PAD_TOP + plotHeight - (tick / 100) * plotHeight"
          :y2="PAD_TOP + plotHeight - (tick / 100) * plotHeight"
        />
        <text
          v-for="tick in [0, 50, 100]"
          :key="`label-${tick}`"
          class="container_task_detail_metrics_tick"
          :x="PAD_LEFT - 6"
          :y="PAD_TOP + plotHeight - (tick / 100) * plotHeight + 4"
          text-anchor="end"
        >{{ tick }}%</text>
        <polygon class="container_task_detail_metrics_area" :points="gpuArea" />
        <polyline class="container_task_detail_metrics_gpu" :points="gpuPoints" />
        <polyline class="container_task_detail_metrics_vram" :points="vramPoints" />
        <text class="container_task_detail_metrics_tick" :x="PAD_LEFT" :y="HEIGHT - 6">
          {{ shortTime(startTime) }}
        </text>
        <text class="container_task_detail_metrics_tick" :x="WIDTH - PAD_RIGHT" :y="HEIGHT - 6" text-anchor="end">
          {{ shortTime(endTime) }}
        </text>
      </svg>
      <div class="container_task_detail_metrics_legend">
        <span class="container_task_detail_metrics_gpukey">
          GPU 利用率{{ latest ? ` ${latest.gpuUtil.toFixed(0)}%` : '' }}
        </span>
        <span class="container_task_detail_metrics_vramkey">
          显存{{ latest && latest.vramTotalMb > 0
            ? ` ${(latest.vramUsedMb / 1024).toFixed(1)} / ${(latest.vramTotalMb / 1024).toFixed(1)} GiB`
            : '' }}
        </span>
        <span v-if="latest?.cpuPercent != null" class="container_task_detail_metrics_cpukey">
          CPU {{ latest.cpuPercent.toFixed(0) }}%
        </span>
      </div>
    </template>
  </section>
</template>

<style>
.container_task_detail_panel_metrics {
  padding: var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-1);
}


.container_task_detail_metrics_empty {
  margin: 0;
  color: var(--c-text-weak);
}

.container_task_detail_metrics_chart {
  display: block;
  width: 100%;
  height: auto;
}

.container_task_detail_metrics_grid {
  stroke: var(--c-divider);
  stroke-width: 1;
}

.container_task_detail_metrics_tick {
  fill: var(--c-text-weak);
  font-size: 11px;
}

.container_task_detail_metrics_area {
  fill: var(--c-brand-bg);
}

.container_task_detail_metrics_gpu {
  fill: none;
  stroke: var(--c-brand);
  stroke-width: 2;
}

.container_task_detail_metrics_vram {
  fill: none;
  stroke: var(--c-warn);
  stroke-width: 1.5;
}

.container_task_detail_metrics_legend {
  display: flex;
  gap: var(--sp-4);
  align-items: center;
  margin-top: var(--sp-3);
  padding-top: var(--sp-2);
  border-top: 1px solid var(--c-divider);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.container_task_detail_metrics_gpukey {
  font-weight: 600;
  color: var(--c-brand);
}

.container_task_detail_metrics_vramkey {
  font-weight: 600;
  color: var(--c-warn);
}

.container_task_detail_metrics_cpukey {
  color: var(--c-text-sub);
}
</style>
