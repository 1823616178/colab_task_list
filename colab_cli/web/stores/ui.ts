import { defineStore } from 'pinia'

// doc/04 §3：ui toast、全局 loading、主题
// P3 落地 toast（至少错误提示）；doc/09 支持白天与夜间主题切换。
export interface Toast {
  id: number
  kind: 'info' | 'warn' | 'error'
  text: string
}

export type Theme = 'dark' | 'light'

let nextToastId = 1

export const useUiStore = defineStore('ui', () => {
  const toasts = ref<Toast[]>([])
  const theme = ref<Theme>('dark')

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

  function applyTheme(t: Theme) {
    if (typeof document !== 'undefined' && document.documentElement) {
      document.documentElement.setAttribute('data-theme', t)
    }
  }

  function initTheme() {
    if (typeof window === 'undefined') return
    try {
      const saved = localStorage.getItem('colab_theme') as Theme | null
      if (saved === 'light' || saved === 'dark') {
        theme.value = saved
      } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
        theme.value = 'light'
      } else {
        theme.value = 'dark'
      }
      applyTheme(theme.value)
    } catch {
      theme.value = 'dark'
      applyTheme('dark')
    }
  }

  function setTheme(t: Theme) {
    theme.value = t
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem('colab_theme', t)
      }
    } catch {
      // 容错
    }
    applyTheme(t)
  }

  function toggleTheme() {
    setTheme(theme.value === 'light' ? 'dark' : 'light')
  }

  return {
    toasts,
    theme,
    remove,
    push,
    info,
    warn,
    error,
    initTheme,
    setTheme,
    toggleTheme,
    applyTheme,
  }
})
