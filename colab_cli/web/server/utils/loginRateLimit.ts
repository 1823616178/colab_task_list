// 登录防爆破：按「IP + 用户名」与「IP」两个维度做滑动窗口计数。
// - 窗口内失败次数达到 maxFailures → 临时锁定 lockoutMs（锁定期内直接 429，不再校验密码）；
// - 未达阈值时按失败次数渐进延迟（progressiveDelayMs），拖慢自动化尝试；
// - 登录成功清空该 IP+用户名 的失败记录。
// 纯内存实现：单实例 Nitro（PM2 单进程）足够；多实例部署需换成共享存储。

export interface LoginRateLimitOptions {
  /** 滑动窗口长度 */
  windowMs: number
  /** 窗口内允许的失败次数（IP+用户名维度） */
  maxFailures: number
  /** 同一 IP 跨用户名的失败上限（防撞库） */
  maxFailuresPerIp: number
  /** 锁定时长 */
  lockoutMs: number
  /** 每次失败追加的延迟基数（第 n 次失败延迟 base*2^(n-1)，封顶 maxDelayMs） */
  delayBaseMs: number
  maxDelayMs: number
}

export const DEFAULT_LOGIN_RATE_LIMIT: LoginRateLimitOptions = {
  windowMs: 60_000,
  maxFailures: 5,
  maxFailuresPerIp: 20,
  lockoutMs: 15 * 60_000,
  delayBaseMs: 250,
  maxDelayMs: 4_000,
}

interface Bucket {
  failures: number[]
  lockedUntil: number
}

export interface RateLimitVerdict {
  allowed: boolean
  /** 锁定剩余毫秒（allowed=false 时有意义） */
  retryAfterMs: number
}

export class LoginRateLimiter {
  private buckets = new Map<string, Bucket>()
  private readonly options: LoginRateLimitOptions
  private readonly now: () => number

  // 不用参数属性：node --test 的类型剥离模式不支持该语法
  constructor(
    options: LoginRateLimitOptions = DEFAULT_LOGIN_RATE_LIMIT,
    now: () => number = Date.now,
  ) {
    this.options = options
    this.now = now
  }

  private key(ip: string, username: string): string {
    return `u:${ip}|${username.toLowerCase()}`
  }

  private ipKey(ip: string): string {
    return `ip:${ip}`
  }

  private bucket(key: string): Bucket {
    let bucket = this.buckets.get(key)
    if (!bucket) {
      bucket = { failures: [], lockedUntil: 0 }
      this.buckets.set(key, bucket)
    }
    const cutoff = this.now() - this.options.windowMs
    bucket.failures = bucket.failures.filter(ts => ts > cutoff)
    return bucket
  }

  /** 登录前检查：任一维度处于锁定期即拒绝 */
  check(ip: string, username: string): RateLimitVerdict {
    const now = this.now()
    let retryAfterMs = 0
    for (const key of [this.key(ip, username), this.ipKey(ip)]) {
      const bucket = this.buckets.get(key)
      if (bucket && bucket.lockedUntil > now) {
        retryAfterMs = Math.max(retryAfterMs, bucket.lockedUntil - now)
      }
    }
    return { allowed: retryAfterMs === 0, retryAfterMs }
  }

  /** 记一次失败；返回本次应施加的延迟与是否因此进入锁定 */
  recordFailure(ip: string, username: string): { delayMs: number, locked: boolean } {
    const now = this.now()
    const user = this.bucket(this.key(ip, username))
    const byIp = this.bucket(this.ipKey(ip))
    user.failures.push(now)
    byIp.failures.push(now)
    let locked = false
    if (user.failures.length >= this.options.maxFailures) {
      user.lockedUntil = now + this.options.lockoutMs
      user.failures = []
      locked = true
    }
    if (byIp.failures.length >= this.options.maxFailuresPerIp) {
      byIp.lockedUntil = now + this.options.lockoutMs
      byIp.failures = []
      locked = true
    }
    const n = Math.max(user.failures.length, 1)
    const delayMs = Math.min(this.options.delayBaseMs * 2 ** (n - 1), this.options.maxDelayMs)
    this.prune(now)
    return { delayMs, locked }
  }

  recordSuccess(ip: string, username: string): void {
    this.buckets.delete(this.key(ip, username))
  }

  /** 防内存膨胀：清掉既无近期失败也未锁定的桶 */
  private prune(now: number): void {
    if (this.buckets.size < 10_000) return
    const cutoff = now - this.options.windowMs
    for (const [key, bucket] of this.buckets) {
      if (bucket.lockedUntil <= now && bucket.failures.every(ts => ts <= cutoff)) {
        this.buckets.delete(key)
      }
    }
  }
}

declare global {
  var __colabLoginRateLimiter: LoginRateLimiter | undefined
}

export function loginRateLimiter(): LoginRateLimiter {
  globalThis.__colabLoginRateLimiter ??= new LoginRateLimiter()
  return globalThis.__colabLoginRateLimiter
}
