// 计算后端（与 colab_core/config.py 的 BACKENDS / BACKEND_GPU_SPECS 保持一致）
export const COMPUTE_BACKENDS = ['colab', 'kaggle', 'modal'] as const
export type ComputeBackend = typeof COMPUTE_BACKENDS[number]

export const BACKEND_LABELS: Record<ComputeBackend, string> = {
  colab: 'Colab',
  kaggle: 'Kaggle',
  modal: 'Modal',
}

export const BACKEND_GPU_SPECS: Record<ComputeBackend, string[]> = {
  colab: ['T4', 'L4', 'G4', 'H100', 'A100'],
  kaggle: ['T4', 'T4x2', 'P100'],
  modal: ['T4', 'L4', 'A10', 'L40S', 'A100', 'A100-80GB', 'H100', 'H200', 'B200'],
}

export const BACKEND_TPU_SPECS: Record<ComputeBackend, string[]> = {
  colab: ['v5e1', 'v6e1'],
  kaggle: [],
  modal: [],
}

export function isComputeBackend(value: unknown): value is ComputeBackend {
  return typeof value === 'string' && (COMPUTE_BACKENDS as readonly string[]).includes(value)
}

export function backendLabel(value: string | null | undefined): string {
  return isComputeBackend(value) ? BACKEND_LABELS[value] : BACKEND_LABELS.colab
}
