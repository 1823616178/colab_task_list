import path from 'node:path'
import { Client } from 'basic-ftp'

const TASK_ID_PATTERN = /^\d{8}-\d{6}-[0-9a-f]{8}$/

export interface FtpUploadConfig {
  host: string
  port: number
  user: string
  password: string
  directory: string
  secure: boolean
  timeoutMs: number
}

export interface FtpClientAdapter {
  access(options: {
    host: string
    port: number
    user: string
    password: string
    secure: boolean
  }): Promise<unknown>
  ensureDir(remoteDirPath: string): Promise<unknown>
  remove(remotePath: string, ignoreError?: boolean): Promise<unknown>
  uploadFrom(localPath: string, remotePath: string): Promise<unknown>
  rename(source: string, destination: string): Promise<unknown>
  close(): void
}

type FtpClientFactory = (timeoutMs: number) => FtpClientAdapter

function normalizeDirectory(value: string): string {
  const segments = value.replace(/\\/g, '/').split('/').filter(Boolean)
  if (segments.length === 0 || segments.some(segment => segment === '.' || segment === '..')) {
    throw new Error('FTP 目录配置无效')
  }
  return segments.join('/')
}

export function ftpRemotePath(directory: string, taskId: string): string {
  if (!TASK_ID_PATTERN.test(taskId)) throw new Error(`任务 ID 非法: ${taskId}`)
  return path.posix.join(normalizeDirectory(directory), `${taskId}.zip`)
}

export function ftpConfigFromRuntime(raw: Record<string, unknown>): FtpUploadConfig | null {
  const host = String(raw.ftpHost || '').trim()
  const user = String(raw.ftpUser || '').trim()
  const password = String(raw.ftpPassword || '')
  if (!host || !user || !password) return null
  const port = Number(raw.ftpPort)
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('FTP 端口必须是 1-65535 的整数')
  }
  return {
    host,
    port,
    user,
    password,
    directory: normalizeDirectory(String(raw.ftpDirectory || 'colab')),
    secure: raw.ftpSecure === true || String(raw.ftpSecure).toLowerCase() === 'true',
    timeoutMs: 30_000,
  }
}

/** 原子上传：先写 .part，再重命名为稳定文件名，历史页不会指向半个 ZIP。 */
export async function uploadOutputFile(
  localPath: string,
  taskId: string,
  config: FtpUploadConfig,
  createClient: FtpClientFactory = timeoutMs => new Client(timeoutMs),
): Promise<string> {
  const remotePath = ftpRemotePath(config.directory, taskId)
  const remoteName = path.posix.basename(remotePath)
  const temporaryName = `${remoteName}.part`
  const client = createClient(config.timeoutMs)
  try {
    await client.access({
      host: config.host,
      port: config.port,
      user: config.user,
      password: config.password,
      secure: config.secure,
    })
    await client.ensureDir(config.directory)
    await client.remove(temporaryName, true)
    await client.uploadFrom(localPath, temporaryName)
    await client.remove(remoteName, true)
    await client.rename(temporaryName, remoteName)
    return remotePath
  } finally {
    client.close()
  }
}
