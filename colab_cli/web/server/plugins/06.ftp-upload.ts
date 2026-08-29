// 启动补传：迁移会把既有真实成功产物标为 pending；上传队列串行执行，
// 不阻塞 Nitro 启动，也不会与任务本地下载按钮互相等待。
import { recoverFtpUploads } from '../services/ftpUpload'

declare global {
  var __colabFtpRecoveryStarted: boolean | undefined
}

export default defineNitroPlugin(() => {
  if (globalThis.__colabFtpRecoveryStarted) return
  globalThis.__colabFtpRecoveryStarted = true
  void recoverFtpUploads()
    .then((count) => {
      if (count > 0) console.log(`[ftp-upload] 已加入 ${count} 个历史/待补传产物`)
    })
    .catch(error => console.warn('[ftp-upload] 启动补传失败:', error))
})
