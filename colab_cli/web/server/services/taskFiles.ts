// tasks 产物文件定位：run.log / output.zip 直读共享目录（doc/01 §6）。
// 防路径穿越：解析后的绝对路径必须落在 runnerOutputDir 内。
import path from 'node:path'
import { repoRoot } from './workflows'

// 恒返回绝对路径：未显式配置 NUXT_RUNNER_OUTPUT_DIR 时回退到
// <repoRoot>/colab_output（Runner 的默认输出根），所以不存在「没有根目录」这一态。
export function taskOutputRoot(): string {
  const config = useRuntimeConfig()
  const configured = String(config.runnerOutputDir || '')
  return path.resolve(configured || path.join(repoRoot(), 'colab_output'))
}

/**
 * 解析任务产物绝对路径：优先 DB 存的路径（Runner 事件回报），
 * 缺省按约定 <outputRoot>/<taskId>/<filename> 组合。
 * 结果必须位于 outputRoot 内，否则返回 null。
 */
export function resolveTaskFilePath(
  storedPath: string | null,
  outputRoot: string,
  taskId: string,
  filename: string,
): string | null {
  const candidate = path.resolve(storedPath || path.join(outputRoot, taskId, filename))
  const root = outputRoot + path.sep
  return candidate.startsWith(root) ? candidate : null
}
