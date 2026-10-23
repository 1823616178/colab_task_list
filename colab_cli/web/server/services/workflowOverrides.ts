import type { EditableWorkflowField } from '../entities/WorkflowConfig'

const MAX_OVERRIDE_FIELDS = 32
const MAX_OVERRIDE_VALUE_LENGTH = 100_000

export class WorkflowOverrideValidationError extends Error {}

/** Missing values use the workflow's default: optional tail frames are off. */
export function normalizeLastFrameOption(raw: unknown, optionalLastFrame: boolean): boolean | undefined {
  if (raw !== undefined && typeof raw !== 'boolean') {
    throw new WorkflowOverrideValidationError('use_last_frame 必须是布尔值')
  }
  if (!optionalLastFrame) {
    if (raw !== undefined) throw new WorkflowOverrideValidationError('当前工作流不支持尾帧开关')
    return undefined
  }
  return raw ?? false
}

/** 读取旧草稿时过滤工作流已删除的字段，保证不会把过期字段交回前台。 */
export function filterWorkflowDraftValues(
  raw: unknown,
  editableFields: EditableWorkflowField[],
): Record<string, string> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const allowedKeys = new Set(editableFields.map(field => field.key))
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).filter(
      (entry): entry is [string, string] => allowedKeys.has(entry[0]) && typeof entry[1] === 'string',
    ),
  )
}

/** 校验浏览器提交的任务级覆盖，只允许工作流配置显式声明的字符串字段。 */
export function normalizeWorkflowOverrides(
  raw: unknown,
  editableFields: EditableWorkflowField[],
): Record<string, string> {
  if (raw === undefined || raw === null) return {}
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    throw new WorkflowOverrideValidationError('workflow_overrides 必须是对象')
  }

  const entries = Object.entries(raw as Record<string, unknown>)
  if (entries.length > MAX_OVERRIDE_FIELDS) {
    throw new WorkflowOverrideValidationError(
      `workflow_overrides 最多允许 ${MAX_OVERRIDE_FIELDS} 个字段`,
    )
  }
  const allowedFields = new Map(editableFields.map(field => [field.key, field]))
  const normalized: Record<string, string> = {}
  for (const [key, value] of entries) {
    const field = allowedFields.get(key)
    if (!field) {
      throw new WorkflowOverrideValidationError(`工作流不支持参数覆盖: ${key}`)
    }
    if (typeof value !== 'string') {
      throw new WorkflowOverrideValidationError(`workflow_overrides.${key} 必须是字符串`)
    }
    if (value.length > MAX_OVERRIDE_VALUE_LENGTH) {
      throw new WorkflowOverrideValidationError(
        `workflow_overrides.${key} 不能超过 ${MAX_OVERRIDE_VALUE_LENGTH} 个字符`,
      )
    }
    if (field.value_type === 'number'
      && (value.trim() === '' || !Number.isFinite(Number(value)))) {
      throw new WorkflowOverrideValidationError(`workflow_overrides.${key} 必须是有效数字`)
    }
    if (field.options?.length
      && !field.options.some(option => option.value === value)) {
      throw new WorkflowOverrideValidationError(
        `workflow_overrides.${key} 不在允许的选项中`,
      )
    }
    normalized[key] = value
  }
  return normalized
}
