import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

/** 执行真实服务代码，仅替换边界依赖，避免测试接触线上数据库/Runner。 */
export function loadService<T>(path: URL, modules: Record<string, unknown>, globals: Record<string, unknown> = {}): T {
  const source = readFileSync(path, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports = {}
  runInNewContext(compiled, {
    exports,
    require: (name: string) => {
      if (!(name in modules)) throw new Error(`缺少测试依赖 ${name}`)
      return modules[name]
    },
    console: { log() {}, warn() {}, error() {} },
    Date, setTimeout, clearTimeout, setInterval, clearInterval,
    AbortController, ReadableStream, TextDecoder, TextEncoder, URL,
    ...globals,
  }, { filename: path.pathname })
  return exports as T
}
