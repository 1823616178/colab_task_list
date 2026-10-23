import { promises as fs } from 'node:fs'

export interface RawWorkflowConfig {
  name?: string
  api_json?: string
  optional_last_frame?: boolean
  supported_accelerators?: string[]
  accelerator_note?: string
  input_fields?: Record<string, unknown>[]
  editable_fields?: Record<string, unknown>[]
  comfy_flags?: string[]
  models?: Record<string, unknown>[]
}

/** Canvas exports may sit beside configs, but cannot be submitted to Runner. */
export async function readWorkflowConfigFile(filePath: string): Promise<RawWorkflowConfig | null> {
  const raw: unknown = JSON.parse(await fs.readFile(filePath, 'utf8'))
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error(`工作流配置必须是对象: ${filePath}`)
  }
  if (Array.isArray((raw as Record<string, unknown>).nodes)) return null
  return raw as RawWorkflowConfig
}
