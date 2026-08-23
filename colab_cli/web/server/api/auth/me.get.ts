import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { toPublicUser } from '../../utils/auth'
import { UserSchema } from '../../entities/User'

// GET /api/auth/me → { user }（doc/02 §3.1）。从库里取最新状态（角色/停用即时生效）
export default defineEventHandler(async (event) => {
  const auth = event.context.auth
  if (!auth) return sendApiError(event, 401, 'UNAUTHORIZED', '未登录或登录已过期')
  const user = await (await getDataSource())
    .getRepository(UserSchema)
    .findOne({ where: { id: auth.userId } })
  if (!user || !user.isActive) {
    return sendApiError(event, 401, 'UNAUTHORIZED', '账号不存在或已停用')
  }
  return { user: toPublicUser(user) }
})
