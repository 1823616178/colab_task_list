import { defineStore } from 'pinia'
import type { TaskRow } from '~/stores/task'
import { browserNotifyText, findLeftTasks } from '~/utils/taskWatch'

// 浏览器桌面通知：用户在顶栏开启后，每 20s 轮询在途任务（queued/running），
// 任务离开在途状态即拉取详情并弹系统通知（点击跳到任务详情）。
// 偏好存 localStorage；浏览器未授权或不支持时自动关闭。
const STORAGE_KEY = 'colab_notify'
const POLL_MS = 20_000

export type NotifyPermission = 'unsupported' | 'default' | 'granted' | 'denied'

export const useTaskNotifyStore = defineStore('taskNotify', () => {
  const enabled = ref(false)
  const permission = ref<NotifyPermission>('default')
  let known: Set<string> | null = null
  let timer: ReturnType<typeof setInterval> | null = null

  function supported(): boolean {
    return typeof window !== 'undefined' && 'Notification' in window
  }

  function init() {
    if (!supported()) {
      permission.value = 'unsupported'
      return
    }
    permission.value = Notification.permission as NotifyPermission
    try {
      enabled.value = localStorage.getItem(STORAGE_KEY) === '1' && permission.value === 'granted'
    } catch {
      enabled.value = false
    }
  }

  async function poll() {
    const { $apiFetch } = useNuxtApp()
    try {
      const [queued, running] = await Promise.all([
        $apiFetch<{ items: TaskRow[] }>('/api/tasks', { query: { status: 'queued', size: 100 } }),
        $apiFetch<{ items: TaskRow[] }>('/api/tasks', { query: { status: 'running', size: 100 } }),
      ])
      const current = new Set([...queued.items, ...running.items].map(task => task.id))
      const left = findLeftTasks(known, current)
      known = current
      for (const id of left) {
        const { task } = await $apiFetch<{ task: TaskRow }>(`/api/tasks/${id}`)
        const text = browserNotifyText(task)
        if (!text) continue
        const notification = new Notification(text.title, { body: text.body, tag: `task-${id}` })
        notification.onclick = () => {
          window.focus()
          void navigateTo(`/tasks/${id}`)
          notification.close()
        }
      }
    } catch {
      // 轮询失败静默：下一轮再试（系统状态由 health store 展示）
    }
  }

  function start() {
    if (!enabled.value || timer) return
    known = null
    void poll()
    timer = setInterval(() => void poll(), POLL_MS)
  }

  function stop() {
    if (timer) clearInterval(timer)
    timer = null
    known = null
  }

  async function toggle(): Promise<NotifyPermission> {
    if (!supported()) {
      permission.value = 'unsupported'
      return permission.value
    }
    if (enabled.value) {
      enabled.value = false
      stop()
    } else {
      if (Notification.permission === 'default') await Notification.requestPermission()
      permission.value = Notification.permission as NotifyPermission
      enabled.value = permission.value === 'granted'
      if (enabled.value) start()
    }
    try {
      localStorage.setItem(STORAGE_KEY, enabled.value ? '1' : '0')
    } catch {
      // 容错
    }
    return permission.value
  }

  return { enabled, permission, init, start, stop, toggle }
})
