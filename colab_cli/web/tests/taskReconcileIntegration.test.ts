import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as rules from '../server/services/sessionReconcileRules.ts'
import * as tracking from '../server/services/taskTracking.ts'
import { loadService } from './helpers/loadService.ts'
import type * as Service from '../server/services/sessionReconcile.ts'

function harness(expectedAlive = true) {
  const task = { id: 'task', sessionName: 's', status: 'running', workflowName: 'test', lastSeq: 80, errorCode: null as string | null }
  const session = { name: 's', status: 'busy', expectedAlive, endpoint: 'ep', createdAt: new Date() }
  const subscriptions: string[] = []
  const stopped: string[] = []
  const finalized: string[] = []
  type Filter = { id?: string, status?: string | string[], errorCode?: string }
  const matches = (where: Filter) => (!where.id || where.id === task.id)
    && (!where.status || (Array.isArray(where.status) ? where.status.includes(task.status) : where.status === task.status))
    && (!where.errorCode || where.errorCode === task.errorCode)
  const taskRepo = {
    find: async ({ where }: { where: Filter }) => matches(where) ? [{ ...task }] : [],
    findOne: async () => null,
    update: async (where: Filter, values: object) => {
      if (!matches(where)) return { affected: 0 }
      Object.assign(task, values)
      return { affected: 1 }
    },
  }
  const sessionRepo = {
    find: async () => [{ ...session }],
    update: async (_where: object, values: object) => { Object.assign(session, values); return { affected: 1 } },
  }
  const ds = {
    getRepository: (schema: string) => schema === 'task' ? taskRepo : sessionRepo,
    query: async (sql: string, args: unknown[]) => {
      if (sql.startsWith('INSERT')) Object.assign(session, { status: args[1], expectedAlive: !!args[2] })
    },
  }
  const service = loadService<typeof Service>(new URL('../server/services/sessionReconcile.ts', import.meta.url), {
    typeorm: { In: (values: string[]) => values },
    '../entities/Session': { SessionSchema: 'session' },
    '../entities/Task': { TaskSchema: 'task', TERMINAL_TASK_STATUSES: ['failed', 'succeeded', 'orphaned'] },
    '../utils/db': { getDataSource: async () => ds },
    './runnerClient': {},
    './taskSubscription': { ensureTaskSubscription: (id: string) => subscriptions.push(id), stopTaskSubscription: (id: string) => stopped.push(id) },
    './taskEvents': { finalizeTaskFromStoredTerminalEvent: async (id: string) => { finalized.push(id); return true } },
    './taskTracking': tracking,
    './sessionReconcileRules': rules,
  })
  return { service, task, session, subscriptions, stopped, finalized }
}

const live = { name: 's', status: 'BUSY', endpoint: 'ep', reachable: true }
const snapshot = { task_id: 'task', session: 's', status: 'running', phase: 'workflow.exec', last_seq: 80 }

test('对账查询暂时失败保留预期，下一轮仍检查并识别 Runner 中断', async () => {
  const h = harness()
  await h.service.runSessionReconcile({ fetchRunnerSessions: async () => [], fetchRunnerTask: async () => { throw Error('timeout') } })
  assert.equal(h.task.status, 'running')
  assert.equal(h.session.expectedAlive, true)
  assert.equal(h.stopped.length, 0)
  await h.service.runSessionReconcile({ fetchRunnerSessions: async () => [], fetchRunnerTask: async () => ({ ...snapshot, status: 'interrupted' }) })
  assert.equal(h.task.status, 'orphaned')
  assert.equal(h.task.errorCode, 'RUNNER_INTERRUPTED')
  assert.deepEqual(h.stopped, ['task'])
})

test('历史 expectedAlive=false 也持续核实在途任务，不会永远卡在 running', async () => {
  const h = harness(false)
  await h.service.runSessionReconcile({ fetchRunnerSessions: async () => [], fetchRunnerTask: async () => ({ ...snapshot, status: 'interrupted' }) })
  assert.equal(h.task.status, 'orphaned')
})

test('VM 仍存活但 Runner 执行线程已中断，正确提示监控中断与无人接管 VM', async () => {
  const h = harness()
  const result = await h.service.runSessionReconcile({ fetchRunnerSessions: async () => [live], fetchRunnerTask: async () => ({ ...snapshot, status: 'interrupted' }) })
  assert.equal(h.task.status, 'orphaned')
  assert.equal(h.task.errorCode, 'RUNNER_INTERRUPTED')
  assert.equal(h.session.expectedAlive, false)
  assert.equal(result.orphans.includes('s'), true)
})

test('VM 列表缺项但 Runner 正常接管时继续订阅，保留会话状态', async () => {
  const h = harness()
  await h.service.runSessionReconcile({ fetchRunnerSessions: async () => [], fetchRunnerTask: async () => snapshot })
  assert.equal(h.task.status, 'running')
  assert.equal(h.session.status, 'busy')
  assert.deepEqual(h.subscriptions, ['task'])
})

test('重取后的 Runner 终态序号更新时补拉新事件，不套用数据库旧失败', async () => {
  const h = harness()
  await h.service.runSessionReconcile({ fetchRunnerSessions: async () => [live], fetchRunnerTask: async () => ({ ...snapshot, status: 'succeeded', last_seq: 90 }) })
  assert.deepEqual(h.finalized, [])
  assert.deepEqual(h.subscriptions, ['task'])
})
