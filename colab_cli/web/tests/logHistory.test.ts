import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mergeLogHistory, parseRunLog } from '../utils/logHistory.ts'

test('run.log 按实时日志相同规则处理连续回车刷新行', () => {
  assert.deepEqual(parseRunLog('启动\n\r进度 10%\n\r进度 20%\n完成\n'), {
    lines: ['启动', '进度 20%', '完成'],
    overwritable: false,
  })
})

test('刷新时合并历史快照和 SSE 新日志，不丢行也不重复', () => {
  assert.deepEqual(
    mergeLogHistory(
      ['创建会话', '挂载 Drive', '下载模型'],
      ['挂载 Drive', '下载模型', '启动 ComfyUI'],
      2000,
    ),
    ['创建会话', '挂载 Drive', '下载模型', '启动 ComfyUI'],
  )
})

test('历史日志合并后仍遵守日志行数上限', () => {
  assert.deepEqual(
    mergeLogHistory(['1', '2', '3'], ['3', '4'], 3),
    ['2', '3', '4'],
  )
})
