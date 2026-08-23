const WORKFLOW_DRAFT_VERSION = 2

interface StoredWorkflowDraft {
  version: 1 | typeof WORKFLOW_DRAFT_VERSION
  values: Record<string, string>
  /** v1 没有时间，仅在数据库尚无草稿时迁移。 */
  updatedAt?: string
}

export interface ParsedWorkflowDraft {
  values: Record<string, string>
  updatedAt: string | null
}

/** 草稿按登录用户和工作流隔离，避免共用浏览器时互相覆盖 Prompt。 */
export function workflowDraftStorageKey(userId: number, workflowSlug: string): string {
  return `colab-console:workflow-draft:${userId}:${workflowSlug}`
}

/**
 * 只恢复当前工作流声明过的字符串字段。
 * 工作流字段删改后，旧草稿中的未知字段不会被继续提交。
 */
export function parseWorkflowDraft(
  raw: string | null,
  allowedKeys: readonly string[],
): Record<string, string> {
  return parseWorkflowDraftRecord(raw, allowedKeys)?.values ?? {}
}

export function parseWorkflowDraftRecord(
  raw: string | null,
  allowedKeys: readonly string[],
): ParsedWorkflowDraft | null {
  if (!raw) return null

  try {
    const parsed = JSON.parse(raw) as Partial<StoredWorkflowDraft> | null
    if (
      !parsed
      || (parsed.version !== 1 && parsed.version !== WORKFLOW_DRAFT_VERSION)
      || !parsed.values
      || typeof parsed.values !== 'object'
      || Array.isArray(parsed.values)
    ) {
      return null
    }

    const allowed = new Set(allowedKeys)
    const values = Object.fromEntries(
      Object.entries(parsed.values).filter(
        (entry): entry is [string, string] => allowed.has(entry[0]) && typeof entry[1] === 'string',
      ),
    )
    const updatedAt = typeof parsed.updatedAt === 'string'
      && Number.isFinite(Date.parse(parsed.updatedAt))
      ? parsed.updatedAt
      : null
    return { values, updatedAt }
  } catch {
    return null
  }
}

export function serializeWorkflowDraft(
  values: Record<string, string>,
  updatedAt = new Date().toISOString(),
): string {
  const draft: StoredWorkflowDraft = {
    version: WORKFLOW_DRAFT_VERSION,
    values,
    updatedAt,
  }
  return JSON.stringify(draft)
}
