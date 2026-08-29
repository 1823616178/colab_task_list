import { defineStore } from 'pinia'

// doc/04 §3：ui toast、全局 loading、主题
// P3 落地 toast（至少错误提示）；globalLoading / theme 留待后续阶段。
export interface Toast {
  id: number
  kind: 'info' | 'warn' | 'error'
  text: string
}

let nextToastId = 1

export const useUiStore = defineStore('ui', () => {
  const toasts = ref<Toast[]>([])

  function remove(id: number) {
    toasts.value = toasts.value.filter(t => t.id !== id)
  }

  function push(kind: Toast['kind'], text: string, timeoutMs = 5000) {
    const toast: Toast = { id: nextToastId++, kind, text }
    toasts.value = [...toasts.value, toast]
    if (timeoutMs > 0) {
      setTimeout(() => remove(toast.id), timeoutMs)
    }
  }

  function info(text: string) {
    push('info', text)
  }

  function warn(text: string) {
    push('warn', text, 8000)
  }

  function error(text: string) {
    push('error', text, 8000)
  }

  return { toasts, remove, push, info, warn, error }
})
