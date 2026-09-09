// 案件包（FE-P-016）纯函数：verify 七步清单态、chain 橙警、敏感文件红框、任务相位。
// chain_ok=false 橙色告警但不阻断（与后端语义一致）；verify.ok=false 才禁止导入。
import type { VerifyResult, VerifyStep } from '../api/endpoints/packageCase'

export type StepTone = 'success' | 'warning' | 'error'

export function stepTone(status: VerifyStep['status']): StepTone {
  if (status === 'pass') return 'success'
  if (status === 'warn') return 'warning'
  return 'error'
}

/** 七步固定顺序（缺失步补 fail 占位，防御后端少返） */
export const STEP_ORDER: VerifyStep['key'][] = [
  'format', 'manifest', 'hash', 'declarations', 'schema', 'chain', 'duckdb',
]

export function orderedSteps(steps: VerifyStep[]): VerifyStep[] {
  const byKey = new Map(steps.map((s) => [s.key, s]))
  return STEP_ORDER.map(
    (key) =>
      byKey.get(key) ?? {
        key,
        label: key,
        status: 'fail' as const,
        detail: '后端未返回该步结果',
      },
  )
}

/** fail 步数量（warn 不计） */
export function failCount(steps: VerifyStep[]): number {
  return steps.filter((s) => s.status === 'fail').length
}

/** 是否允许导入：仅当 verify.ok（无 fail 步）；chain warn 不拦 */
export function canImport(v: VerifyResult | null): boolean {
  return !!v && v.ok === true
}

/** chain 橙色告警：chain_ok=false（导出预检 summary.health.chain_ok 同语义） */
export function chainWarning(chainOk: boolean): string {
  return chainOk ? '' : '审计链不完整：导出/导入可继续，但请在交接说明中标注（橙色告警）'
}

/** 敏感文件红框名单（去重保序） */
export function sensitiveRedList(files: string[] | undefined): string[] {
  return Array.from(new Set(files ?? []))
}

/** 导出/导入任务相位（供 TaskProgressCard 文案） */
export type PackagePhase = 'queued' | 'running' | 'done' | 'failed' | 'idle'

export function taskPhase(status: string | undefined): PackagePhase {
  switch (status) {
    case 'PENDING':
      return 'queued'
    case 'RUNNING':
      return 'running'
    case 'SUCCEEDED':
      return 'done'
    case 'FAILED':
    case 'CANCELLED':
      return 'failed'
    default:
      return 'idle'
  }
}

/** 下载文件名（后端 FileResponse filename 模式：{cid}_package.zip） */
export function downloadZipName(caseId: string): string {
  return `${caseId}_package.zip`
}
