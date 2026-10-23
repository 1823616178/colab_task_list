<script setup lang="ts">
import { extractApiError, useAuthStore } from '~/stores/auth'
import { useUiStore } from '~/stores/ui'
import { useWorkflowStore } from '~/stores/workflow'
import type { ModelItem } from '~/stores/workflow'

// 提交任务页的工作流列表（doc/04 §2）+ 模型清单（doc/05 P5）。
// 列表/模型清单 viewer 可读；「同步」与「设首尾帧」限 operator+（与 API 一致）。

const authStore = useAuthStore()
const workflowStore = useWorkflowStore()
const uiStore = useUiStore()

const isOperator = computed(() =>
  authStore.user?.role === 'admin' || authStore.user?.role === 'operator')

// ── 模型清单 ────────────────────────────────────────────────
const modelFilter = ref('')

const manualModels = computed(() => workflowStore.models.filter(model => model.manual))

/** 手工放置目标路径（doc/03 §3：MyDrive/code/backup/models/<comfyDir>/） */
function manualPath(model: ModelItem): string {
  const name = (model.cachePath ?? model.filename).split('/').pop() ?? model.filename
  return `MyDrive/code/backup/models/${model.comfyDir}/${name}`
}

const SOURCE_LABELS: Record<string, string> = {
  drive_cache: 'Drive 缓存',
  huggingface: 'HuggingFace',
  vm_local: 'VM 本地',
}

function sourceLabel(model: ModelItem): string {
  if (model.manual) return '无（手工放置）'
  return model.lastSource ? (SOURCE_LABELS[model.lastSource] ?? model.lastSource) : '—'
}

function presentLabel(model: ModelItem): string {
  if (model.manual) return '需手工放置'
  return model.present ? '已命中' : '未命中'
}

async function onModelFilterChange() {
  try {
    await workflowStore.fetchModels(modelFilter.value || undefined)
  } catch (err) {
    uiStore.error(extractApiError(err, '加载模型清单失败'))
  }
}

async function onSync() {
  try {
    const res = await workflowStore.sync()
    uiStore.info(`已同步 ${res.synced} 个工作流：${res.slugs.join('、')}`)
    await workflowStore.fetchModels(modelFilter.value || undefined)
  } catch (err) {
    uiStore.error(extractApiError(err, '同步工作流失败'))
  }
}

async function refresh() {
  try {
    await Promise.all([
      workflowStore.fetchWorkflows(),
      workflowStore.fetchModels(modelFilter.value || undefined),
    ])
  } catch (err) {
    uiStore.error(extractApiError(err, '加载工作流数据失败'))
  }
}

onMounted(async () => {
  if (workflowStore.workflows.length === 0) {
    await refresh()
    return
  }
  try {
    await workflowStore.fetchModels(modelFilter.value || undefined)
  } catch (err) {
    uiStore.error(extractApiError(err, '加载模型清单失败'))
  }
})
</script>

<template>
  <div class="container_workflow_list_body_root">
    <div class="container_workflow_list_toolbar_root">
      <h2 class="container_workflow_list_header_title">工作流</h2>
      <button
        v-if="isOperator"
        class="container_workflow_list_toolbar_sync"
        type="button"
        :disabled="workflowStore.syncing"
        @click="onSync"
      >
        {{ workflowStore.syncing ? '同步中…' : '从文件同步' }}
      </button>
      <button
        class="container_workflow_list_toolbar_refresh"
        type="button"
        :disabled="workflowStore.loading"
        @click="refresh"
      >
        {{ workflowStore.loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <p v-if="!isOperator" class="container_workflow_list_form_denied">
      当前角色为只读：可查看工作流与模型清单；同步、设首尾帧需 operator 权限。
    </p>

    <div class="container_workflow_list_table_scroll">
      <table class="container_workflow_list_body_table">
        <thead>
          <tr class="container_workflow_list_row_head">
            <th class="container_workflow_list_cell_head">名称</th>
            <th class="container_workflow_list_cell_head">Slug</th>
            <th class="container_workflow_list_cell_head">加速器支持</th>
            <th class="container_workflow_list_cell_head">模型总体积</th>
            <th class="container_workflow_list_cell_head">同步时间</th>
            <th v-if="isOperator" class="container_workflow_list_cell_head">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="workflowStore.workflows.length === 0 && !workflowStore.loading" class="container_workflow_list_row_item">
            <td class="container_workflow_list_cell_empty" :colspan="isOperator ? 6 : 5">
              暂无工作流（可点击「从文件同步」从 workflows/*.json 导入）
            </td>
          </tr>
          <tr
            v-for="workflow in workflowStore.workflows"
            :key="workflow.slug"
            class="container_workflow_list_row_item"
          >
            <td class="container_workflow_list_cell_name">{{ workflow.name }}</td>
            <td class="container_workflow_list_cell_slug">{{ workflow.slug }}</td>
            <td
              class="container_workflow_list_cell_accelerator"
              :title="workflow.acceleratorNote ?? ''"
            >
              {{ workflow.supportedAccelerators.map(kind => kind.toUpperCase()).join(' / ') || '—' }}
            </td>
            <td class="container_workflow_list_cell_size">{{ formatBytes(workflow.totalModelBytes) }}</td>
            <td class="container_workflow_list_cell_synced">{{ formatLocalTime(workflow.syncedAt) }}</td>
            <td v-if="isOperator" class="container_workflow_list_cell_action">
              <NuxtLink
                v-if="workflow.inputImages?.some(image => image.label === '首帧' || image.label === '尾帧')"
                class="container_workflow_list_cell_frames"
                :to="`/workflows/${workflow.slug}/frames`"
              >
                设首尾帧
              </NuxtLink>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="container_workflow_models_toolbar_root">
      <h2 class="container_workflow_models_header_title">模型清单</h2>
      <select
        v-model="modelFilter"
        class="container_workflow_models_filter_select"
        aria-label="按工作流筛选模型"
        @change="onModelFilterChange"
      >
        <option value="">全部工作流</option>
        <option v-for="workflow in workflowStore.workflows" :key="workflow.slug" :value="workflow.slug">
          {{ workflow.name }}（{{ workflow.slug }}）
        </option>
      </select>
    </div>

    <div v-if="manualModels.length > 0" class="container_workflow_models_card_manual" role="alert">
      <p class="container_workflow_models_card_lead">
        以下模型无法从 HuggingFace 获取，必须<strong>手工放置到 Google Drive</strong>
        （放置后任务运行时经 Drive 缓存命中）：
      </p>
      <ul class="container_workflow_models_card_paths">
        <li v-for="model in manualModels" :key="model.filename">
          <code>{{ manualPath(model) }}</code>
        </li>
      </ul>
    </div>

    <div class="container_workflow_models_table_scroll">
      <table class="container_workflow_models_body_table">
        <thead>
          <tr class="container_workflow_models_row_head">
            <th class="container_workflow_models_cell_head">模型（repo / 文件）</th>
            <th class="container_workflow_models_cell_head">目录</th>
            <th class="container_workflow_models_cell_head">体积</th>
            <th class="container_workflow_models_cell_head">缓存命中</th>
            <th class="container_workflow_models_cell_head">来源</th>
            <th class="container_workflow_models_cell_head">最近验证</th>
            <th class="container_workflow_models_cell_head">归属工作流</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="workflowStore.models.length === 0 && !workflowStore.modelsLoading" class="container_workflow_models_row_item">
            <td class="container_workflow_models_cell_empty" colspan="7">暂无模型数据</td>
          </tr>
          <tr
            v-for="model in workflowStore.models"
            :key="`${model.repo ?? ''}|${model.filename}`"
            class="container_workflow_models_row_item"
            :data-manual="model.manual ? 'true' : 'false'"
            :data-present="model.present ? 'true' : 'false'"
          >
            <td class="container_workflow_models_cell_model">
              <span v-if="model.repo" class="container_workflow_models_cell_repo">{{ model.repo }}</span>
              <span class="container_workflow_models_cell_filename">{{ model.filename }}</span>
            </td>
            <td class="container_workflow_models_cell_dir">{{ model.comfyDir }}</td>
            <td class="container_workflow_models_cell_size">{{ formatBytes(model.sizeBytes) }}</td>
            <td
              class="container_workflow_models_cell_present"
              :data-manual="model.manual ? 'true' : 'false'"
              :data-present="model.present ? 'true' : 'false'"
              :title="model.manual ? `须手工放置到 ${manualPath(model)}` : (model.present ? '' : '未命中缓存，下次任务将从 HuggingFace 下载')"
            >
              {{ presentLabel(model) }}
            </td>
            <td class="container_workflow_models_cell_source">{{ sourceLabel(model) }}</td>
            <td class="container_workflow_models_cell_verified">{{ formatLocalTime(model.lastVerifiedAt) }}</td>
            <td class="container_workflow_models_cell_workflows">
              {{ model.workflows.join('、') || '—（仅缓存残留）' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style>
.container_workflow_list_table_scroll {
  overflow-x: auto;
}

.container_workflow_models_table_scroll {
  overflow-x: auto;
}

.container_workflow_list_body_root {
  max-width: var(--w-page);
  margin: 0 auto;
}

.container_workflow_list_toolbar_root {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin-bottom: var(--sp-4);
}

.container_workflow_list_header_title {
  margin: 0;
  margin-right: auto;
  font-size: var(--fs-xl);
}

.container_workflow_list_toolbar_sync {
  padding: 6px 14px;
  font-weight: 600;
  color: var(--c-text-invert);
  background: var(--c-brand);
  border: none;
  border-radius: var(--r-sm);
  box-shadow: var(--sh-glow-brand);
  transition: filter var(--t-fast);
}

.container_workflow_list_toolbar_sync:hover:not(:disabled) {
  filter: brightness(1.1);
}

.container_workflow_list_form_denied {
  padding: 10px var(--sp-4);
  color: var(--c-text-sub);
  background: var(--c-surface-hover);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.container_workflow_list_body_table,
.container_workflow_models_body_table {
  width: 100%;
  min-width: 720px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  border-collapse: collapse;
  box-shadow: var(--sh-1);
}

.container_workflow_list_cell_empty,
.container_workflow_models_cell_empty {
  color: var(--c-text-weak);
  text-align: center;
}

.container_workflow_list_cell_slug,
.container_workflow_models_cell_dir {
  font-family: var(--ff-mono);
  font-size: var(--fs-sm);
}

.container_workflow_list_cell_frames {
  color: var(--c-brand);
  text-decoration: none;
}

.container_workflow_models_toolbar_root {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin: 28px 0 var(--sp-3);
}

.container_workflow_models_header_title {
  margin: 0;
  margin-right: auto;
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--c-text);
}

.container_workflow_models_filter_select {
  padding: 6px 10px;
  color: var(--c-text);
  background: var(--c-surface-sub);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}

.container_workflow_models_card_manual {
  padding: var(--sp-3) var(--sp-4);
  margin: 0 0 var(--sp-3);
  color: var(--c-warn-text);
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-glow-warn);
}

.container_workflow_models_card_lead {
  margin: 0 0 var(--sp-2);
}

.container_workflow_models_card_paths {
  padding-left: 20px;
  margin: 0;
}

.container_workflow_models_card_paths code {
  font-size: var(--fs-sm);
}

.container_workflow_models_cell_model {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.container_workflow_models_cell_repo {
  color: var(--c-text-weak);
  font-size: var(--fs-xs);
}

.container_workflow_models_cell_filename {
  font-family: var(--ff-mono);
  font-size: var(--fs-sm);
  word-break: break-all;
}

.container_workflow_models_row_item[data-manual='true'] {
  background: var(--c-warn-bg);
  box-shadow: inset 3px 0 0 var(--c-warn);
}

.container_workflow_models_cell_present[data-present='true'] {
  color: var(--c-success);
}

.container_workflow_models_cell_present[data-present='false'][data-manual='false'] {
  color: var(--c-warn-text);
}

.container_workflow_models_cell_present[data-manual='true'] {
  color: var(--c-warn-text);
  font-weight: 600;
}
</style>
