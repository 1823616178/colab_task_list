// 浏览器通知用：对比两次轮询的在途任务 id 集合，找出「刚离开在途状态」的任务。
// 首次轮询（previous=null）只建立基线，不通知，避免刷新页面时刷屏。
export function findLeftTasks(previous: Set<string> | null, current: Set<string>): string[] {
  if (previous === null) return []
  return [...previous].filter(id => !current.has(id))
}

const TITLES: Record<string, string> = {
  succeeded: '任务成功',
  failed: '任务失败',
  recoverable: '任务失败（VM 存活，可重取）',
  canceled: '任务已取消',
  orphaned: '任务已失联',
}

/** 只对终态生成通知文案；仍在途（如被重新排队）返回 null */
export function browserNotifyText(task: {
  id: string
  status: string
  workflowName: string
  errorMessage: string | null
}): { title: string, body: string } | null {
  const title = TITLES[task.status]
  if (!title) return null
  const error = task.errorMessage ? `\n${task.errorMessage.replace(/\s+/g, ' ').slice(0, 120)}` : ''
  return { title: `${title}：${task.workflowName}`, body: `任务 ${task.id}${task.status === 'succeeded' ? '' : error}` }
}
