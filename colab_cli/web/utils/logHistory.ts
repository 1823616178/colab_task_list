export interface ParsedRunLog {
  lines: string[]
  overwritable: boolean
}

/** 按实时日志相同的 \r 覆盖规则解析磁盘 run.log。 */
export function parseRunLog(text: string): ParsedRunLog {
  const entries = text.split('\n')
  if (entries.length > 0 && entries[entries.length - 1] === '') entries.pop()
  const lines: string[] = []
  let overwritable = false
  for (const entry of entries) {
    const segments = entry.split('\r')
    const visible = segments[segments.length - 1] ?? ''
    if (segments.length > 1 && overwritable && lines.length > 0) {
      lines[lines.length - 1] = visible
    } else {
      lines.push(visible)
    }
    overwritable = segments.length > 1
  }
  return { lines, overwritable }
}

/**
 * 合并磁盘快照与加载快照期间 SSE 收到的日志。
 * 两者边界通常有重叠，取“历史后缀 = 实时前缀”的最大重叠以免重复。
 */
export function mergeLogHistory(
  historical: string[],
  live: string[],
  cap: number,
): string[] {
  const maxOverlap = Math.min(historical.length, live.length)
  let overlap = 0
  for (let size = maxOverlap; size > 0; size--) {
    let matches = true
    const historicalStart = historical.length - size
    for (let index = 0; index < size; index++) {
      if (historical[historicalStart + index] !== live[index]) {
        matches = false
        break
      }
    }
    if (matches) {
      overlap = size
      break
    }
  }
  return historical.concat(live.slice(overlap)).slice(-cap)
}
