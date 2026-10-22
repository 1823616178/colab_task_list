<script setup lang="ts">
// 首/尾帧拖拽上传区（/workflows/:slug/frames 用，doc/04 §2）。
// 拖放或点选图片；data-dragover 表达悬停态（doc/04 §1.3）。
const props = defineProps<{
  label: string
  hint?: string
  modelValue: File | null
}>()

const emit = defineEmits<{
  'update:modelValue': [file: File | null]
}>()

const dragover = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)

const previewUrl = ref<string | null>(null)
const fileError = ref('')

function setFile(file: File | null) {
  if (file && !/\.(png|jpe?g|webp|bmp|gif|tiff?)$/i.test(file.name)) {
    fileError.value = '仅支持 PNG、JPEG、WebP、BMP、GIF、TIFF 图片'
    return
  }
  fileError.value = ''
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = file ? URL.createObjectURL(file) : null
  emit('update:modelValue', file)
}

watch(() => props.modelValue, (file) => {
  if (file || !previewUrl.value) return
  URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = null
})

function onDrop(event: DragEvent) {
  dragover.value = false
  setFile(event.dataTransfer?.files?.[0] ?? null)
}

function onInputChange(event: Event) {
  setFile((event.target as HTMLInputElement).files?.[0] ?? null)
  // 允许重复选择同一文件（重置 input 值）
  if (inputRef.value) inputRef.value.value = ''
}

onBeforeUnmount(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})

const fileName = computed(() => props.modelValue?.name ?? '')
</script>

<template>
  <div
    class="container_workflow_frames_drop_zone"
    :data-dragover="dragover ? 'true' : 'false'"
    :data-filled="modelValue ? 'true' : 'false'"
    @dragover.prevent="dragover = true"
    @dragleave.prevent="dragover = false"
    @drop.prevent="onDrop"
    @click="inputRef?.click()"
  >
    <input
      ref="inputRef"
      class="container_workflow_frames_drop_input"
      type="file"
      accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.tif,.tiff"
      @change="onInputChange"
    >
    <img
      v-if="previewUrl"
      class="container_workflow_frames_drop_preview"
      :src="previewUrl"
      :alt="`${label}预览`"
    >
    <span class="container_workflow_frames_drop_label">
      {{ modelValue ? fileName : `拖放${label}到这里，或点击选择` }}
    </span>
    <span v-if="hint" class="container_workflow_frames_drop_hint">{{ hint }}</span>
    <span v-if="fileError" class="container_workflow_frames_drop_hint" role="alert">{{ fileError }}</span>
    <button
      v-if="modelValue"
      class="container_workflow_frames_drop_clear"
      type="button"
      @click.stop="setFile(null)"
    >
      移除
    </button>
  </div>
</template>

<style>
.container_workflow_frames_drop_zone {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  align-items: center;
  justify-content: center;
  min-height: 160px;
  padding: var(--sp-4);
  cursor: pointer;
  background: var(--c-surface-sub);
  border: 2px dashed var(--c-border-strong);
  border-radius: var(--r-md);
  transition:
    background-color var(--t-base),
    border-color var(--t-base),
    box-shadow var(--t-base);
}

.container_workflow_frames_drop_zone[data-dragover='true'] {
  background: var(--c-brand-bg);
  border-color: var(--c-brand);
  box-shadow: var(--sh-glow-brand);
}

.container_workflow_frames_drop_zone[data-filled='true'] {
  border-style: solid;
  border-color: var(--c-success);
}

.container_workflow_frames_drop_input {
  display: none;
}

.container_workflow_frames_drop_preview {
  max-width: 100%;
  max-height: 180px;
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
  box-shadow: var(--sh-1);
  object-fit: contain;
}

.container_workflow_frames_drop_label {
  color: var(--c-text-sub);
  word-break: break-all;
}

.container_workflow_frames_drop_hint {
  color: var(--c-warn-text);
  font-size: var(--fs-xs);
  text-align: center;
}

.container_workflow_frames_drop_clear {
  padding: 2px 10px;
  color: var(--c-danger);
  background: var(--c-surface);
  border: 1px solid var(--c-danger);
  border-radius: var(--r-sm);
}

</style>
