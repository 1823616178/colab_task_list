/** 只有处理成功的事件才能推进续订水位；失败必须让调用方重连并重放。 */
export async function deliverTaskEvent<T extends { task_id: string, seq: number }>(
  cursor: { taskId: string, lastSeq: number },
  event: T,
  commit: (event: T) => Promise<void>,
  publish: (event: T) => void,
): Promise<boolean> {
  if (event.task_id !== cursor.taskId || !Number.isSafeInteger(event.seq) || event.seq < 0) {
    throw new Error('无效的任务事件标识或序号')
  }
  if (event.seq <= cursor.lastSeq) return false
  await commit(event)
  cursor.lastSeq = event.seq
  publish(event)
  return true
}
