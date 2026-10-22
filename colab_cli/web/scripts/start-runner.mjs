#!/usr/bin/env node
// 用 Node 拉起 Python Runner（Windows 默认 python，其他平台默认 python3）。
// token / 输出目录与 web/.env 对齐：NUXT_RUNNER_TOKEN → COLAB_RUNNER_TOKEN。
import { spawn } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const envPath = resolve(webRoot, '.env')

function loadDotEnv(path) {
  if (!existsSync(path)) return
  for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const eq = trimmed.indexOf('=')
    if (eq < 1) continue
    const key = trimmed.slice(0, eq).trim()
    let value = trimmed.slice(eq + 1).trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1)
    }
    if (process.env[key] === undefined) process.env[key] = value
  }
}

loadDotEnv(envPath)

const token = process.env.NUXT_RUNNER_TOKEN || process.env.COLAB_RUNNER_TOKEN
if (!token) {
  console.error('错误: 未设置 NUXT_RUNNER_TOKEN（写在 web/.env）')
  process.exit(1)
}

const repoRoot = resolve(process.env.NUXT_REPO_ROOT || resolve(webRoot, '..'))
if (!existsSync(repoRoot) || !statSync(repoRoot).isDirectory()
  || !existsSync(resolve(repoRoot, 'colab_runner', '__main__.py'))) {
  console.error(`错误: NUXT_REPO_ROOT=${repoRoot} 不是有效的 colab_cli 目录（应包含 colab_runner/__main__.py）。请修改 web/.env，或删除 NUXT_REPO_ROOT 使用默认目录。`)
  process.exit(1)
}
const outputDir = process.env.NUXT_RUNNER_OUTPUT_DIR
  || process.env.COLAB_OUTPUT_DIR
  || resolve(repoRoot, 'colab_output')
const port = process.env.COLAB_RUNNER_PORT || '8765'
const python = process.env.COLAB_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')

const child = spawn(python, ['-m', 'colab_runner'], {
  cwd: repoRoot,
  env: {
    ...process.env,
    COLAB_RUNNER_TOKEN: token,
    COLAB_OUTPUT_DIR: outputDir,
    COLAB_RUNNER_PORT: port,
  },
  stdio: 'inherit',
})

// 信号转发：本脚本只是个壳，真正干活的是 python 子进程。
// PM2 stop/restart 把信号发给壳进程的 pid，不转发的话 python 会变成孤儿 ——
// 它手里可能正攥着一台在计费的 Colab VM，必须让它自己走完清理流程。
// 收到信号后不立刻 exit，等 child 的 exit 回调决定退出码。
let stopping = false
for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(signal, () => {
    if (stopping) return
    stopping = true
    child.kill(signal)
  })
}

child.on('error', (error) => {
  console.error(`无法启动 ${python} -m colab_runner:`, error.message)
  if (error.code === 'ENOENT') {
    console.error('请确认 Python 解释器可用；可先激活 Conda 环境，或设置 COLAB_PYTHON 为 python.exe 的完整路径。')
  }
  process.exit(1)
})
child.on('exit', (code, signal) => {
  if (signal) process.exit(1)
  process.exit(code ?? 1)
})
