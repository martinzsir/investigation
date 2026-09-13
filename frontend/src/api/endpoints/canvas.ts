import { api } from '../client'
import { noteDataVersion } from '../query-keys'
import type {
  AdoptEnvelope,
  CanvasChatEnvelope,
  CanvasDoc,
  CanvasEnvelope,
  CanvasNode,
  CanvasSuggestionEnvelope,
  EdgeMutateEnvelope,
  ExpandDirection,
  ExpandEnvelope,
  FunctionCatalogEnvelope,
  FunctionQueryEnvelope,
  ManualNodeKind,
  NodeDeleteEnvelope,
  NodeMutateEnvelope,
  RollbackEnvelope,
  RuleAudit,
  SnapshotCreateEnvelope,
  SnapshotListEnvelope,
  SuggestionEnvelope,
  SuggestionSyncEnvelope,
  ToVerifyEnvelope,
} from '../../domain/canvas'

// 线索研判画布端点（server/app/routers/canvas.py 契约）：
// GET 惰性 seed（首次进入创建，后续回读已保存文档）；
// PATCH 整文档自动保存（带 version 基准；过期 409，由调用方提示后覆盖重试）；
// POST expand RC-103 逐层溯源（懒加载，服务端幂等合并落库）；
// GET  rules/.../audit RC-102 规则审计视图（只读，按需加载）。
// M3 RC-202/203/206：nodes/edges 结构 CRUD（逐动作审计，乐观锁）+ 快照/回滚。

const base = (caseId: string, clueId: string) =>
  `/cases/${encodeURIComponent(caseId)}/clues/${encodeURIComponent(clueId)}/canvas`

export interface CanvasSaveResult {
  envelope: CanvasEnvelope
  /** true=服务端版本已前进（409 冲突后确认覆盖），调用方应以返回文档替换本地 */
  conflicted: boolean
}

export const canvasApi = {
  /** GET .../canvas —— 取画布（不存在时后端惰性 seed） */
  async get(caseId: string, clueId: string): Promise<CanvasEnvelope> {
    const res = await api.get<CanvasEnvelope>(base(caseId, clueId))
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * PATCH .../canvas —— 防抖自动保存。
   * baseVersion 为本地文档基准版本；409 时若 overwrite=true 以服务端
   * 当前版本重试一次（RC-205：后写覆盖并提示，冲突计入审计）。
   */
  async save(
    caseId: string,
    clueId: string,
    doc: CanvasDoc,
    baseVersion: number,
    overwrite = false,
  ): Promise<CanvasSaveResult> {
    const path = base(caseId, clueId)
    try {
      const res = await api.patch<CanvasEnvelope>(path, {
        doc,
        version: baseVersion,
      })
      noteDataVersion(caseId, res.dataVersion)
      return { envelope: res.data, conflicted: false }
    } catch (e) {
      if (!overwrite) throw e
      // 409：以最新版本为基准重试一次（整文档后写覆盖）
      const latest = await canvasApi.get(caseId, clueId)
      const res = await api.patch<CanvasEnvelope>(path, {
        doc,
        version: latest.version,
      })
      noteDataVersion(caseId, res.dataVersion)
      return { envelope: res.data, conflicted: true }
    }
  },

  /**
   * POST .../canvas/expand —— RC-103 展开节点下一层。
   * 返回新整文档（服务端幂等合并后）+ 本次新增节点/边 + 抽屉字段负载。
   */
  async expand(
    caseId: string,
    clueId: string,
    nodeId: string,
    direction: ExpandDirection,
    version: number,
  ): Promise<ExpandEnvelope> {
    const res = await api.post<ExpandEnvelope>(base(caseId, clueId) + '/expand', {
      node_id: nodeId,
      direction,
      version,
    })
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * GET .../canvas/rules/{ruleId}/audit —— RC-102 规则审计视图数据。
   * 404=历史占位规则/声明缺失，由抽屉展示缺失态，不当作系统错误。
   */
  async ruleAudit(caseId: string, ruleId: string): Promise<RuleAudit> {
    const res = await api.get<RuleAudit>(
      `/cases/${encodeURIComponent(caseId)}/canvas/rules/${encodeURIComponent(ruleId)}/audit`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ------------------------------------------------------------------
  // M3 RC-202：人工节点（假设/备注）
  // ------------------------------------------------------------------
  async createNode(
    caseId: string,
    clueId: string,
    body: {
      kind: ManualNodeKind
      props: CanvasNode['props']
      x: number
      y: number
      version: number
    },
  ): Promise<NodeMutateEnvelope> {
    const res = await api.post<NodeMutateEnvelope>(base(caseId, clueId) + '/nodes', body)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async updateNode(
    caseId: string,
    clueId: string,
    nodeId: string,
    body: { props: CanvasNode['props']; version: number },
  ): Promise<NodeMutateEnvelope> {
    const res = await api.patch<NodeMutateEnvelope>(
      `${base(caseId, clueId)}/nodes/${encodeURIComponent(nodeId)}`,
      body,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async deleteNode(
    caseId: string,
    clueId: string,
    nodeId: string,
    version: number,
  ): Promise<NodeDeleteEnvelope> {
    const res = await api.delete<NodeDeleteEnvelope>(
      `${base(caseId, clueId)}/nodes/${encodeURIComponent(nodeId)}?version=${version}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ------------------------------------------------------------------
  // M3 RC-203：人工连线
  // ------------------------------------------------------------------
  async createEdge(
    caseId: string,
    clueId: string,
    body: {
      source: string
      target: string
      rel: string
      note?: string | null
      version: number
    },
  ): Promise<EdgeMutateEnvelope> {
    const res = await api.post<EdgeMutateEnvelope>(base(caseId, clueId) + '/edges', body)
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  async deleteEdge(
    caseId: string,
    clueId: string,
    edgeId: string,
    version: number,
  ): Promise<EdgeMutateEnvelope> {
    const res = await api.delete<EdgeMutateEnvelope>(
      `${base(caseId, clueId)}/edges/${encodeURIComponent(edgeId)}?version=${version}`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ------------------------------------------------------------------
  // M3 RC-206：快照（不可变）与回滚
  // ------------------------------------------------------------------
  async listSnapshots(
    caseId: string,
    clueId: string,
  ): Promise<SnapshotListEnvelope> {
    const res = await api.get<SnapshotListEnvelope>(
      base(caseId, clueId) + '/snapshots',
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** 创建快照：立即落库、非防抖；不推进画布 version。 */
  async createSnapshot(
    caseId: string,
    clueId: string,
    label: string,
    version: number,
  ): Promise<SnapshotCreateEnvelope> {
    const res = await api.post<SnapshotCreateEnvelope>(
      base(caseId, clueId) + '/snapshots',
      { label, version },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** 回滚：服务端先存「回滚前自动恢复点」，再整体替换画布。 */
  async rollbackSnapshot(
    caseId: string,
    clueId: string,
    snapshotId: string,
    version: number,
  ): Promise<RollbackEnvelope> {
    const res = await api.post<RollbackEnvelope>(
      `${base(caseId, clueId)}/snapshots/${encodeURIComponent(snapshotId)}/rollback`,
      { version },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ------------------------------------------------------------------
  // M4 RC-105：手册核实建议（pb: 虚节点，不写 state/不进工作台）
  // ------------------------------------------------------------------
  /** 规则节点「生成手册核实建议」；无新增时版本不变。 */
  async generateSuggestions(
    caseId: string,
    clueId: string,
    nodeId: string,
    version: number,
  ): Promise<SuggestionEnvelope> {
    const res = await api.post<SuggestionEnvelope>(
      `${base(caseId, clueId)}/suggestions`,
      { node_id: nodeId, version },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * 采纳手册建议 → 202 入队 TASK_VERIFY（add_manual/transition）；
   * state 已有同文本已处理项时 200（task=null, mode=adopted）。
   * 202 ≠ 成功：调用方必须 waitForTerminal 后再 sync。
   */
  async adoptSuggestion(
    caseId: string,
    clueId: string,
    nodeId: string,
    body: { text?: string; version: number },
  ): Promise<AdoptEnvelope> {
    const res = await api.post<AdoptEnvelope>(
      `${base(caseId, clueId)}/suggestions/${encodeURIComponent(nodeId)}/adopt`,
      body,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** 终态后协调：pb→vi 迁移 / 假设→新增待核实节点；pending=无假成功。 */
  async syncSuggestions(
    caseId: string,
    clueId: string,
    targets: Array<{ node_id: string; text?: string }>,
    version: number,
  ): Promise<SuggestionSyncEnvelope> {
    const res = await api.post<SuggestionSyncEnvelope>(
      `${base(caseId, clueId)}/suggestions/sync`,
      { targets, version },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /** 人工假设「转为待核实」→ 202 add_manual；终态后 sync 生成节点。 */
  async hypothesisToVerify(
    caseId: string,
    clueId: string,
    nodeId: string,
    text: string,
    version: number,
  ): Promise<ToVerifyEnvelope> {
    const res = await api.post<ToVerifyEnvelope>(
      `${base(caseId, clueId)}/nodes/${encodeURIComponent(nodeId)}/to-verify`,
      { text, version },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * M4 P3：通用「添加人工核查」入队（fact / object / source_row 抽屉采纳建议）。
   * 与 hypothesisToVerify 走同一接口 + 同一幂等口径（按 text 字符串 idem）；
   * 命名上不再绑 hypothesis，方便非假设节点走这条路径。
   */
  async addManualVerify(
    caseId: string,
    clueId: string,
    nodeId: string,
    text: string,
    version: number,
  ): Promise<ToVerifyEnvelope> {
    return canvasApi.hypothesisToVerify(caseId, clueId, nodeId, text, version)
  },

  // ------------------------------------------------------------------
  // M4 RC-204：白名单只读 Function 扩展查询
  // ------------------------------------------------------------------
  /** 工具箱扩展查询目录（业务化表单，不暴露 sql/impl）。 */
  async listFunctions(
    caseId: string,
    clueId: string,
  ): Promise<FunctionCatalogEnvelope> {
    const res = await api.get<FunctionCatalogEnvelope>(
      `${base(caseId, clueId)}/functions`,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * 执行扩展查询：成功落 function_result 节点（executed=true）；
   * 数据源未接入/降级返回 200 executed=false（不产生节点）。
   */
  async functionQuery(
    caseId: string,
    clueId: string,
    body: {
      function: string
      params: Record<string, unknown>
      source_node_id?: string | null
      version: number
    },
  ): Promise<FunctionQueryEnvelope> {
    const res = await api.post<FunctionQueryEnvelope>(
      `${base(caseId, clueId)}/function-query`,
      body,
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  // ------------------------------------------------------------------
  // M5 RC-301：画布只读问答（RC-302 引用校验串联）
  // ------------------------------------------------------------------
  /** 就当刻画布提问，返回带引用回答（facts 有据 / pending 待核实）。 */
  async chat(
    caseId: string,
    clueId: string,
    question: string,
  ): Promise<CanvasChatEnvelope> {
    const res = await api.post<CanvasChatEnvelope>(
      base(caseId, clueId) + '/chat',
      { question },
      // LLM 推理常 >15s（实测约 17s），走跨案长超时档 60s
      { timeoutKind: 'cross' },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },

  /**
   * RC-303：把问答产出的核实建议提交为提案（draft 态）。
   * 不直接落画布/核查项；审批通过后由既有桥接生成 TASK_VERIFY。
   */
  async submitSuggestion(
    caseId: string,
    clueId: string,
    text: string,
  ): Promise<CanvasSuggestionEnvelope> {
    const res = await api.post<CanvasSuggestionEnvelope>(
      base(caseId, clueId) + '/chat/suggestion',
      { text },
    )
    noteDataVersion(caseId, res.dataVersion)
    return res.data
  },
}
