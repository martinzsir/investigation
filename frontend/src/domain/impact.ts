// S5-F3 影响面展示领域逻辑（纯函数、确定性可测）。
// 五类口径（PRD §8.2）：clues🔴 已生成线索（R2 已固证单独标）、
// rules🟡 规则（含阈值策略/核查剧本）、views🟡 视图、
// tables🟢 已物化表（发布后需重跑 BUILD）、functions⚪ 函数。
// D10：status=failed 禁止发布；partial 显式列出失败类与原因，允许带警示发布。
import type {
  ImpactCategoryKey,
  ImpactItem,
  ImpactResult,
  ProposalDto,
  ProposalStatus,
} from '../api/endpoints/ontologyGeneric'

export interface CategoryMeta {
  key: ImpactCategoryKey
  label: string
  /** naive-ui NTag type */
  color: 'error' | 'warning' | 'success' | 'default'
  hint: string
}

export const CATEGORY_META: CategoryMeta[] = [
  { key: 'clues', label: '已生成线索', color: 'error', hint: '删除/改名后，历史线索的溯源自此失效（不会自动迁移）' },
  { key: 'rules', label: '规则', color: 'warning', hint: '规则/阈值策略/核查剧本仍引用被删项' },
  { key: 'views', label: '视图', color: 'warning', hint: '角色投影视图的基对象或投影列受影响' },
  { key: 'tables', label: '已物化表', color: 'success', hint: '语义表/列已物化，发布后需要重跑 BUILD 才一致' },
  { key: 'functions', label: '函数', color: 'default', hint: '只读 Function 的结构化引用或 SQL 文本命中' },
]

export const CATEGORY_LABEL: Record<ImpactCategoryKey, string> = {
  clues: '已生成线索',
  rules: '规则',
  views: '视图',
  tables: '已物化表',
  functions: '函数',
}

export const PROPOSAL_STATUS_META: Record<ProposalStatus, { label: string; color: 'default' | 'info' | 'success' | 'error' }> = {
  draft: { label: '草稿（未评估影响面）', color: 'default' },
  impact_ready: { label: '影响面已评估，待人工发布', color: 'info' },
  published: { label: '已发布', color: 'success' },
  discarded: { label: '已废弃', color: 'error' },
}

export function itemsOf(impact: ImpactResult, key: ImpactCategoryKey): ImpactItem[] {
  return impact.categories[key]?.items ?? []
}

export function categoryUnavailable(impact: ImpactResult, key: ImpactCategoryKey): boolean {
  return impact.categories[key]?.status === 'unavailable'
}

export function allItems(impact: ImpactResult): ImpactItem[] {
  return CATEGORY_META.flatMap((m) => itemsOf(impact, m.key))
}

export function solidifiedClues(impact: ImpactResult): ImpactItem[] {
  return itemsOf(impact, 'clues').filter((i) => i.solidified)
}

export function uncertainItems(impact: ImpactResult): ImpactItem[] {
  return allItems(impact).filter((i) => i.certainty === 'uncertain')
}

/** 🔴 D10：全失败（或有类不可用导致整体 failed）→ 禁止发布（不得当作无影响）。 */
export function impactBlocksPublish(impact: ImpactResult | null | undefined): boolean {
  return !impact || impact.status === 'failed'
}

/** 发布门槛（E4-1 由后端硬拦，前端同口径禁用按钮）：impact_ready 且影响面非 failed。 */
export function canPublishProposal(p: ProposalDto | null | undefined): boolean {
  return Boolean(p && p.status === 'impact_ready' && p.impact && !impactBlocksPublish(p.impact))
}

export function canDiscardProposal(p: ProposalDto | null | undefined): boolean {
  return Boolean(p && (p.status === 'draft' || p.status === 'impact_ready'))
}

export interface BlockReason {
  level: 'danger' | 'warning'
  text: string
}

/** 汇总发布前必须让用户看到的警示（§8.3 已固证 / §8.4 不确定 / D10 部分失败）。 */
export function impactWarnings(impact: ImpactResult): BlockReason[] {
  const out: BlockReason[] = []
  const locked = solidifiedClues(impact)
  if (locked.length) {
    out.push({
      level: 'danger',
      text: `🔴 ${locked.length} 条已固证/已立案线索的溯源将受影响。固证证据不可静默失效，发布前请逐条人工评估并留痕（系统不会自动迁移线索）。`,
    })
  }
  const uncertain = uncertainItems(impact)
  if (uncertain.length) {
    out.push({
      level: 'warning',
      text: `🟡 ${uncertain.length} 项为不确定引用（规则文本/动态 SQL 词法命中，无法静态确定）。按「宁可多报不可漏报」原则一并列出，需人工核对。`,
    })
  }
  if (impact.dynamic_refs.length) {
    out.push({
      level: 'warning',
      text: `🟡 bindings.json 的动态 SQL 中有 ${impact.dynamic_refs.length} 处词法命中（${impact.dynamic_refs
        .map((d) => d.token)
        .join('、')}），影响面无法解析这类引用，请人工核对 source_sql/build_sql。`,
    })
  }
  for (const f of impact.failures) {
    out.push({ level: 'danger', text: `⛔ ${f.category === 'all' ? '影响面计算失败' : `${CATEGORY_LABEL[f.category as ImpactCategoryKey] ?? f.category}不可用`}：${f.reason}（不可当作无影响）` })
  }
  const layer = impact.standard_layer
  if (layer?.layer === 'shared' || layer?.layer === 'industry') {
    const who = layer.layer === 'shared' ? '全域层' : `行业层（${layer.industry ?? ''}）`
    out.push({
      level: 'warning',
      text: `⚠ 当前编辑案件层文件，同名文件存在于${who}。本次发布仅影响本案件；${layer.affected_cases > 0 ? `若到标准层修改将影响 ${layer.affected_cases} 个案件。` : ''}`,
    })
  }
  return out
}

/** 影响面为空（五类零命中、零不确定、零失败）时的覆盖范围说明（§8.7）。 */
export function isZeroImpact(impact: ImpactResult): boolean {
  return (
    impact.status === 'complete' &&
    (impact.totals.total ?? 0) === 0 &&
    impact.dynamic_refs.length === 0 &&
    impact.failures.length === 0
  )
}
