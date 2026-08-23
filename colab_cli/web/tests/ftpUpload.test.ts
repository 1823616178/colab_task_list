import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  ftpConfigFromRuntime,
  ftpRemotePath,
  uploadOutputFile,
} from '../server/services/ftpTransfer.ts'
import type { FtpClientAdapter, FtpUploadConfig } from '../server/services/ftpTransfer.ts'

const TASK_ID = '20260823-120000-abcdef12'

class FakeFtpClient implements FtpClientAdapter {
  calls: string[] = []
  closed = false

  async access(): Promise<void> { this.calls.push('access') }
  async ensureDir(directory: string): Promise<void> { this.calls.push(`ensure:${directory}`) }
  async remove(remotePath: string): Promise<void> { this.calls.push(`remove:${remotePath}`) }
  async uploadFrom(localPath: string, remotePath: string): Promise<void> {
    this.calls.push(`upload:${localPath}:${remotePath}`)
  }
  async rename(source: string, destination: string): Promise<void> {
    this.calls.push(`rename:${source}:${destination}`)
  }
  close(): void { this.closed = true }
}

const CONFIG: FtpUploadConfig = {
  host: '192.0.2.10',
  port: 21,
  user: 'ftp-user',
  password: 'secret',
  directory: 'colab',
  secure: false,
  timeoutMs: 30_000,
}

test('FTP 远端路径固定在配置目录并使用任务 ID 命名', () => {
  assert.equal(ftpRemotePath('/colab/', TASK_ID), `colab/${TASK_ID}.zip`)
  assert.throws(() => ftpRemotePath('../outside', TASK_ID), /FTP 目录配置无效/)
  assert.throws(() => ftpRemotePath('colab', 'bad-id'), /任务 ID 非法/)
})

test('FTP 配置缺少秘密值时禁用，合法配置完成类型转换', () => {
  assert.equal(ftpConfigFromRuntime({ ftpHost: 'host', ftpUser: 'user' }), null)
  const parsed = ftpConfigFromRuntime({
    ftpHost: ' host ',
    ftpPort: '21',
    ftpUser: ' user ',
    ftpPassword: 'pass',
    ftpDirectory: '/colab/',
    ftpSecure: 'false',
  })
  assert.deepEqual(parsed, {
    ...CONFIG,
    host: 'host',
    user: 'user',
    password: 'pass',
  })
})

test('FTP 上传先写临时文件再原子改名，并始终关闭连接', async () => {
  const client = new FakeFtpClient()
  const remotePath = await uploadOutputFile(
    '/tmp/output.zip',
    TASK_ID,
    CONFIG,
    () => client,
  )
  assert.equal(remotePath, `colab/${TASK_ID}.zip`)
  assert.deepEqual(client.calls, [
    'access',
    'ensure:colab',
    `remove:${TASK_ID}.zip.part`,
    `upload:/tmp/output.zip:${TASK_ID}.zip.part`,
    `remove:${TASK_ID}.zip`,
    `rename:${TASK_ID}.zip.part:${TASK_ID}.zip`,
  ])
  assert.equal(client.closed, true)
})
