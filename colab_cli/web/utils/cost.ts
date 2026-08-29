// 计算单元预估（doc/05 P4）：费率表为代码内常量 —— **近似值，以 Colab 账单为准**。
// 数值参考 Colab Pro 实测（2024-10，units/h：T4≈1.6 / L4≈3.0 / A100≈10.6 / TPU≈1.8），
// H100 无公开实测，按 A100 之上估 13；费率随 Google 定价调整可能漂移，只用于量级感知。

const VARIANT_RATES: Record<string, number> = {
  T4: 1.6,
  L4: 3.0,
  A100: 10.6,
  H100: 13.0,
}
const TPU_RATE = 1.8
const CPU_RATE = 0.1

/** 该 variant/accelerator 每小时计算单元近似费率；未知返回 null（UI 显示 —） */
export function computeUnitRatePerHour(
  variant: string | null | undefined,
  accelerator: string | null | undefined,
): number | null {
  const text = (variant ?? '').toUpperCase()
  for (const [key, rate] of Object.entries(VARIANT_RATES)) {
    if (text.includes(key)) return rate
  }
  if (accelerator === 'tpu' || text.includes('TPU') || text.includes('V5E') || text.includes('V6E')) {
    return TPU_RATE
  }
  if (accelerator === 'cpu') return CPU_RATE
  return null
}

/** 预估消耗 = 费率 × 存活时长（小时）；缺费率或存活时长返回 null */
export function estimateComputeUnits(
  variant: string | null | undefined,
  accelerator: string | null | undefined,
  uptimeMs: number | null,
): number | null {
  const rate = computeUnitRatePerHour(variant, accelerator)
  if (rate === null || uptimeMs === null || uptimeMs < 0) return null
  return (rate * uptimeMs) / 3_600_000
}

export function formatComputeUnits(value: number | null): string {
  if (value === null) return '—'
  return `≈${value >= 10 ? value.toFixed(0) : value.toFixed(1)} 单元`
}
