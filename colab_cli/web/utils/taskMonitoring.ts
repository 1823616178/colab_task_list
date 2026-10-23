export function finiteMetric(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export interface WorkflowProgress {
  ts: string
  text: string
  currentNode: string | null
  completedNodes: number | null
  totalNodes: number | null
  percent: number | null
  step: number | null
  steps: number | null
}

export function workflowProgress(event: {
  ts: string, message: string, data?: Record<string, unknown>
}): WorkflowProgress {
  const data = event.data ?? {}
  return {
    ts: event.ts,
    text: typeof data.text === 'string' ? data.text : event.message,
    currentNode: typeof data.current_node === 'string' ? data.current_node : null,
    completedNodes: finiteMetric(data.completed_nodes),
    totalNodes: finiteMetric(data.total_nodes),
    percent: finiteMetric(data.percent),
    step: finiteMetric(data.step),
    steps: finiteMetric(data.steps),
  }
}

export function sampleIsStale(ts: string | null, waitingSince: number, now: number): boolean {
  const time = ts ? Date.parse(ts) : waitingSince
  return Number.isFinite(time) && now - time > 90_000
}
