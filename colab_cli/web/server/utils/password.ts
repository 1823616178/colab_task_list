import argon2 from 'argon2'

// doc/03 §2：argon2id（优于 bcrypt）。参数取 OWASP 推荐基线（m=19MiB, t=2, p=1）。
export async function hashPassword(plain: string): Promise<string> {
  return argon2.hash(plain, {
    type: argon2.argon2id,
    memoryCost: 19456,
    timeCost: 2,
    parallelism: 1,
  })
}

export async function verifyPassword(hash: string, plain: string): Promise<boolean> {
  try {
    return await argon2.verify(hash, plain)
  } catch {
    return false // hash 串损坏等异常情况一律视为校验失败
  }
}
