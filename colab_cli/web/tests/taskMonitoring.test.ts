import assert from 'node:assert/strict'
import { test } from 'node:test'
import { deliverTaskEvent } from '../server/services/taskEventDelivery.ts'
import { createTaskBrowserStream } from '../server/services/taskBrowserStream.ts'
import { finiteMetric, sampleIsStale, workflowProgress } from '../utils/taskMonitoring.ts'

const event = (seq: number, phase = 'workflow.exec') => ({ task_id: 'task', seq, phase })
const frame = (seq: number, phase?: string) => `event: task\ndata: ${JSON.stringify(event(seq, phase))}\n\n`
const decode = (value: Uint8Array | undefined) => new TextDecoder().decode(value)

test('写库失败不推进水位、不广播，重放成功后再确认终态；重复事件只处理一次', async () => {
  const cursor = { taskId: 'task', lastSeq: 80 }
  let commits = 0
  const published: number[] = []
  const commit = async () => {
    commits++
    if (commits === 1) throw new Error('database unavailable')
  }
  const publish = (e: { seq: number }) => published.push(e.seq)
  await assert.rejects(deliverTaskEvent(cursor, event(81, 'task.done'), commit, publish))
  assert.equal(cursor.lastSeq, 80)
  assert.deepEqual(published, [])
  await deliverTaskEvent(cursor, event(81, 'task.done'), commit, publish)
  await deliverTaskEvent(cursor, event(81, 'task.done'), commit, publish)
  assert.equal(cursor.lastSeq, 81)
  assert.equal(commits, 2)
  assert.deepEqual(published, [81])
  await assert.rejects(deliverTaskEvent(cursor, { ...event(82), task_id: 'other' }, commit, publish))
})

test('重试下载从头回放旧失败后保持连接，接收新进度和当前终态', async () => {
  let send = (_frame: string) => {}
  let unsubscribed = 0
  let ensured = 0
  const { stream } = createTaskBrowserStream({
    taskId: 'task', fromSeq: -1,
    subscribe: push => { send = push; return () => { unsubscribed++ } },
    loadHistory: async () => [event(5, 'task.failed'), event(6, 'output.transfer')],
    loadTask: async () => ({ status: 'running', lastSeq: 6 }),
    ensureSubscription: () => { ensured++ },
  })
  const reader = stream.getReader()
  assert.match(decode((await reader.read()).value), /event: replay/)
  assert.match(decode((await reader.read()).value), /output.transfer/)
  assert.match(decode((await reader.read()).value), /event: snapshot/)
  assert.equal(unsubscribed, 0)
  assert.equal(ensured, 1)
  send(frame(7, 'output.transfer'))
  assert.match(decode((await reader.read()).value), /"seq":7/)
  send(frame(8, 'task.done'))
  assert.match(decode((await reader.read()).value), /task.done/)
  assert.equal((await reader.read()).done, true)
  assert.equal(unsubscribed, 1)
})

test('多次失败后成功的历史完整回放，不在第一次失败处截断', async () => {
  const { stream } = createTaskBrowserStream({
    taskId: 'task', fromSeq: -1, subscribe: () => () => {},
    loadHistory: async () => [event(5, 'task.failed'), event(8, 'task.failed'), event(12, 'task.done')],
    loadTask: async () => ({ status: 'succeeded', lastSeq: 12 }),
    ensureSubscription: () => { assert.fail('terminal task must not subscribe') },
  })
  const text = await new Response(stream).text()
  assert.equal((text.match(/event: replay/g) ?? []).length, 3)
  assert.match(text, /"status":"succeeded"/)
})

test('回放与实时交界处去重，缓冲中的当前终态仍能完成收尾', async () => {
  let send = (_frame: string) => {}
  const { stream } = createTaskBrowserStream({
    taskId: 'task', fromSeq: 4,
    subscribe: push => { send = push; return () => {} },
    loadHistory: async () => { send(frame(5)); send(frame(6, 'task.done')); return [event(5)] },
    loadTask: async () => ({ status: 'succeeded', lastSeq: 6 }),
    ensureSubscription: () => {},
  })
  const text = await new Response(stream).text()
  assert.equal((text.match(/"seq":5/g) ?? []).length, 1)
  assert.equal((text.match(/"seq":6/g) ?? []).length, 1)
})

test('回放期间断开立即注销浏览器；数据库失败不悄悄跳过回放', async () => {
  let release = (_rows: ReturnType<typeof event>[]) => {}
  let unsubscribed = 0
  const deps = {
    taskId: 'task', fromSeq: -1,
    subscribe: () => () => { unsubscribed++ },
    loadHistory: () => new Promise<ReturnType<typeof event>[]>(resolve => { release = resolve }),
    loadTask: async () => ({ status: 'running', lastSeq: -1 }),
    ensureSubscription: () => {},
  }
  const first = createTaskBrowserStream(deps)
  first.close()
  assert.equal(unsubscribed, 1)
  release([])
  assert.equal((await first.stream.getReader().read()).done, true)
  const failed = createTaskBrowserStream({ ...deps, loadHistory: async () => { throw new Error('DB down') } })
  await assert.rejects(new Response(failed.stream).text(), /DB down/)
  assert.equal(unsubscribed, 2)
})

test('进度保留当前节点、节点总数与内部步数；缺失资源值不误报零', () => {
  const progress = workflowProgress({ ts: '2026-10-22T17:00:00Z', message: '', data: {
    current_node: 'KSampler', completed_nodes: 3, total_nodes: 10, percent: 30, step: 2, steps: 4,
  } })
  assert.equal(progress.currentNode, 'KSampler')
  assert.equal(progress.step, 2)
  assert.equal(progress.steps, 4)
  assert.equal(progress.percent, 30)
  assert.equal(finiteMetric(null), null)
  assert.equal(finiteMetric(''), null)
  assert.equal(finiteMetric(0), 0)
})

test('资源指标按采样时间判定过期，心跳与页面刷新不会让旧数据变新', () => {
  const start = Date.parse('2026-10-22T17:00:00Z')
  assert.equal(sampleIsStale(new Date(start).toISOString(), start + 100000, start + 100000), true)
  assert.equal(sampleIsStale(null, start, start + 90001), true)
  assert.equal(sampleIsStale(null, start, start + 30000), false)
})
