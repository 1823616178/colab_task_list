import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'

test('白天与夜间双皮肤主题 token 完整对齐', async () => {
  const cssPath = path.resolve(import.meta.dirname, '../assets/css/main.css')
  const css = await readFile(cssPath, 'utf8')

  // 提取夜间与白天块中的 CSS 变量
  const darkBlockMatch = css.match(/:root,\s*:root\[data-theme='dark'\]\s*\{([^}]+)\}/)
  const lightBlockMatch = css.match(/:root\[data-theme='light'\]\s*\{([^}]+)\}/)

  assert.ok(darkBlockMatch, 'main.css 必须包含 :root[data-theme="dark"] 规则块')
  assert.ok(lightBlockMatch, 'main.css 必须包含 :root[data-theme="light"] 规则块')

  const extractVars = (block: string) => {
    const vars: string[] = []
    const regex = /(--[a-zA-Z0-9_-]+)\s*:/g
    let m: RegExpExecArray | null
    while ((m = regex.exec(block)) !== null) {
      vars.push(m[1])
    }
    return vars.sort()
  }

  const darkVars = extractVars(darkBlockMatch[1])
  const lightVars = extractVars(lightBlockMatch[1])

  // 确保白天与夜间 token 变量集合完全一致
  assert.deepEqual(
    lightVars,
    darkVars,
    '白天模式 (light) 与夜间模式 (dark) 的设计 token 必须完全对齐',
  )

  // 核心语义 token 必须包含
  const requiredTokens = [
    '--c-brand',
    '--c-text',
    '--c-text-invert',
    '--c-bg',
    '--c-surface',
    '--c-surface-sub',
    '--c-border',
    '--c-running',
    '--c-success',
    '--c-danger',
    '--c-warn',
    '--c-log-bg',
    '--sh-1',
    '--sh-2',
    '--btn-filter-hover',
    '--bg-pattern',
  ]

  for (const token of requiredTokens) {
    assert.ok(darkVars.includes(token), `缺失关键 token: ${token}`)
  }
})
