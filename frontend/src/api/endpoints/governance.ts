import { api } from '../client'
import { noteDataVersion } from '../query-keys'

// S4 治理建模（server/app/routers/governance.py 契约）。
// actions.json：Action Type（only_from/target_status/terminal/requires_role/
//   parameters/side_effects），案件快照整体替换；
// states.json：状态 + 迁移表（{from:[to]} map 往返），案件快照整体替换。
// 🔴 terminal/requires_role（actions）与取消终态标记（states）变更：
//   后端强制非空理由 + 审计留痕，前端危险确认只是 UX，门禁在服务端。

/** 动作角色门禁（core.ontology_loader.ALLOWED_ROLES） */
export type ActionRole = 'any' | 'human'

export interface ActionParam {
  name: string
  type?: string
  required?: boolean
  description?: string
  [k: string]: unknown
}

/** 动作声明（actions.json 数组项；保留全部原始键，未知键经 _unknown_keys 提示） */
export interface ActionDecl {
  name: string
  title?: string
  target_status: string
  derive?: string
  /** 显式收紧的前置状态（非空数组；缺省=按状态迁移表反推可达来源） */
  only_from?: string[]
  parameters: ActionParam[]
  requires_role: ActionRole
  terminal: boolean
  side_effects: string[]
  description?: string
  /** GET 附带：界面未覆盖键（保存时剥离，不落盘） */
  _unknown_keys?: string[]
  [k: string]: unknown
}

export interface GovernanceEnums {
  requires_role: string[]
  side_effects: string[]
  derive: string[]
}

export interface ActionListResult {
  actions: ActionDecl[]
  pack: string
  enums: GovernanceEnums
  state_names: string[]
}

export interface DangerChange {
  name: string
  title: string
  field: 'terminal' | 'requires_role'
  before: string
  after: string
}

export interface StateDecl {
  name: string
  label?: string
  tone?: string
  terminal: boolean
  requires_role?: string
  requires_basis?: boolean
  sla_days?: number
  outcome?: string
  _unknown_keys?: string[]
  [k: string]: unknown
}

export interface StateListResult {
  states: StateDecl[]
  /** {from: [to,...]} 迁移表 */
  transitions: Record<string, string[]>
  /** 状态 → 引用它的动作名（target_status/only_from） */
  referenced_by: Record<string, string[]>
  pack: string
}

export const governanceApi = {
  /** GET /cases/{cid}/actions */
  async listActions(caseId: string): Promise<ActionListResult> {
    const res = await api.get<ActionListResult>(
      `/cases/${encodeURIComponent(caseId)}/actions`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/actions 🔴 危险字段变更需非空 reason（后端强制） */
  async saveActions(
    caseId: string,
    actions: ActionDecl[],
    reason?: string,
  ): Promise<{ saved: boolean; danger_changes: DangerChange[] }> {
    const body: { actions: ActionDecl[]; reason?: string } = { actions }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: boolean; danger_changes: DangerChange[] }>(
      `/cases/${encodeURIComponent(caseId)}/actions`,
      body,
      { idempotencyAction: 'actions-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** GET /cases/{cid}/states */
  async listStates(caseId: string): Promise<StateListResult> {
    const res = await api.get<StateListResult>(
      `/cases/${encodeURIComponent(caseId)}/states`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** PUT /cases/{cid}/states 🔴 R3 终态出边/取消终态由后端强制 */
  async saveStates(
    caseId: string,
    states: StateDecl[],
    transitions: Record<string, string[]>,
    reason?: string,
  ): Promise<{ saved: boolean; unterminal: string[] }> {
    const body: { states: StateDecl[]; transitions: Record<string, string[]>; reason?: string } = {
      states,
      transitions,
    }
    if (reason) body.reason = reason
    const res = await api.put<{ saved: boolean; unterminal: string[] }>(
      `/cases/${encodeURIComponent(caseId)}/states`,
      body,
      { idempotencyAction: 'states-save' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
