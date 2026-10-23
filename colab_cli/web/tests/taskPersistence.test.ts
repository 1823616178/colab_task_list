import assert from 'node:assert/strict'
import { test } from 'node:test'
import { loadService } from './helpers/loadService.ts'
import type * as Service from '../server/services/taskEvents.ts'

function harness(failure: string | null) {
  const initial = { lastSeq: 80, status: 'running', currentPhase: 'workflow.exec', errorMessage: 'old failure' }
  let state = { task: { ...initial }, events: [] as object[] }
  const ds = {
    transaction: async (work: (manager: object) => Promise<unknown>) => {
      const draft = structuredClone(state)
      const fail = (stage: string) => { if (failure === stage) { failure = null; throw new Error(stage) } }
      const manager = {
        getRepository: (schema: string) => ({
          findOne: async () => ({ ...draft.task }),
          update: async (_filter: object, values: object) => { fail('task'); Object.assign(draft.task, values) },
          createQueryBuilder: () => {
            let values: object = {}
            const builder = {
              insert: () => builder, update: () => builder, orIgnore: () => builder,
              values: (v: object) => { values = v; return builder },
              set: (v: object) => { values = v; return builder },
              where: () => builder,
              execute: async () => {
                if (schema === 'event') { fail('insert'); draft.events.push(values) }
                else { fail('terminal'); Object.assign(draft.task, values) }
                return { affected: 1, raw: { affectedRows: 1 } }
              },
            }
            return builder
          },
        }),
        query: async (_sql: string, args: number[]) => { fail('checkpoint'); draft.task.lastSeq = args[0]! },
      }
      const result = await work(manager)
      fail('commit')
      state = draft
      return result
    },
  }
  const service = loadService<typeof Service>(new URL('../server/services/taskEvents.ts', import.meta.url), {
    typeorm: { In: (v: unknown) => v },
    '../entities/Task': { TaskSchema: 'task' },
    '../entities/TaskEvent': { TaskEventSchema: 'event' },
    '../utils/db': { getDataSource: async () => ds },
  })
  return { service, read: () => state, initial }
}

const done: Service.RunnerTaskEvent = {
  v: 1, task_id: 'task', seq: 81, ts: '2026-10-22T17:00:00Z', phase: 'task.done', status: 'ok', message: '',
  data: { output_zip: '/output.zip', size_bytes: 123, transport: 'native' },
}

for (const stage of ['insert', 'terminal', 'checkpoint', 'commit']) {
  test(`事件事务在 ${stage} 失败时整体回滚，重放后事件、终态、水位一起提交`, async () => {
    const h = harness(stage)
    await assert.rejects(h.service.persistMilestoneEvent(done))
    assert.deepEqual(h.read(), { task: h.initial, events: [] })
    await h.service.persistMilestoneEvent(done)
    assert.equal(h.read().task.status, 'succeeded')
    assert.equal(h.read().task.lastSeq, 81)
    assert.equal(h.read().task.errorMessage, null)
    assert.equal(h.read().events.length, 1)
    // 旧 task.start 与同一个终态均不能覆盖新状态。
    await h.service.persistMilestoneEvent({ ...done, seq: 79, phase: 'task.start', status: 'start' })
    await h.service.persistMilestoneEvent(done)
    assert.equal(h.read().task.status, 'succeeded')
    assert.equal(h.read().events.length, 1)
  })
}
