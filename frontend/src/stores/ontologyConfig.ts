import { defineStore } from 'pinia'
import {
  ontologyConfigApi,
  type OntologyConfig,
} from '../api/endpoints/ontologyConfig'

/**
 * Ontology 配置 store（六项解耦共性基建）。
 * - 按案件缓存 GET /cases/{cid}/ontology-config；
 * - 拉取失败/未加载时回落 DEFAULT_ONTOLOGY_CONFIG（双轨过渡：首屏不阻塞、
 *   旧后端无该端点时界面仍可用）；
 * - 本 store 只存原始声明，语义 helper（受控终态/动作现算/列序）在 domain 层。
 */

/**
 * 内置默认包快照——与 ontology/default/*.json 对齐，仅作兜底。
 * 权威值以服务端下发为准；改动默认包声明时同步此处。
 */
export const DEFAULT_ONTOLOGY_CONFIG: OntologyConfig = {
  pack: 'default',
  jians: [
    { name: '因间', default_clearance: 1, weight: 3, source_object_types: ['org', 'bid_project'] },
    { name: '内间', default_clearance: 3, weight: 5, source_object_types: ['tipoff'] },
    { name: '反间', default_clearance: 1, weight: 2, source_object_types: ['transaction'] },
    { name: '死间', default_clearance: 0, weight: 4, source_object_types: ['osint_article', 'org'] },
    { name: '生间', default_clearance: 1, weight: 1, source_object_types: ['call', 'trackpoint'] },
  ],
  cross_levels: [
    { min_independent_sources: 1, name: '观察' },
    { min_independent_sources: 2, name: '线索' },
    { min_independent_sources: 3, name: '可立案依据候选' },
  ],
  dimensions: [
    { name: '资金', note: '银行流水/资金往来异常', source_object_types: ['transaction'] },
    { name: '通讯', note: '通话/联系频次与对象', source_object_types: ['call'] },
    { name: '行为', note: '轨迹同框/行为模式', source_object_types: ['trackpoint'] },
    { name: '关系', note: '工商/社会关系网络', source_object_types: ['person', 'org'] },
    { name: '时间', note: '时间耦合/先后顺序（跨对象属性）', source_object_types: ['transaction', 'call', 'trackpoint'] },
  ],
  states: [
    { name: '待查', label: '待查', tone: 'warning', terminal: false, requires_role: 'any', requires_basis: false, sla_days: 3 },
    { name: '查证中', label: '查证中', tone: 'info', terminal: false, requires_role: 'any', requires_basis: false, sla_days: 5 },
    { name: '已排除', label: '已排除', tone: 'muted', terminal: false, requires_role: 'any', requires_basis: true, outcome: 'excluded' },
    { name: '已固证', label: '已固证', tone: 'success', terminal: false, requires_role: 'any', requires_basis: true, sla_days: 7, outcome: 'verified' },
    { name: '已立案', label: '已立案', tone: 'danger', terminal: true, requires_role: 'human', requires_basis: true },
  ],
  transitions: {
    待查: ['查证中', '已排除', '已固证'],
    查证中: ['待查', '已排除', '已固证'],
    已排除: ['待查'],
    已固证: ['已排除', '已立案'],
    已立案: [],
  },
  actions: [
    {
      name: 'verify', title: '开始查证', target_status: '查证中',
      allowed_from: ['待查'], requires_role: 'any', terminal: false, parameters: [],
    },
    {
      name: 'reset', title: '退回待查', target_status: '待查',
      allowed_from: ['查证中', '已排除'], requires_role: 'any', terminal: false, parameters: [],
    },
    {
      name: 'exclude', title: '排除线索', target_status: '已排除',
      allowed_from: ['待查', '查证中', '已固证'], requires_role: 'any', terminal: false,
      parameters: [{ name: 'reason', type: 'string', required: true, description: '排除理由' }],
    },
    {
      // D1：前端更严——待查态不可直接固证，必须先查证中
      name: 'confirm', title: '固证', target_status: '已固证',
      allowed_from: ['待查', '查证中'], requires_role: 'any', terminal: false, only_from: ['查证中'],
      parameters: [],
    },
    {
      name: 'file', title: '立案', target_status: '已立案',
      allowed_from: ['已固证'], requires_role: 'human', terminal: true,
      parameters: [{ name: 'legal_basis', type: 'string', required: true, description: '立案法定依据' }],
    },
  ],
  scoring: {
    dimensions: [
      { name: 'confidence', weight: 0.4 },
      { name: 'jian_coverage', weight: 0.35 },
      { name: 'data_strength', weight: 0.25 },
    ],
    assumption_confidence: { _default: 0.7 },
  },
}

export const useOntologyConfigStore = defineStore('ontologyConfig', {
  state: () => ({
    byCase: {} as Record<string, OntologyConfig>,
    loading: {} as Record<string, boolean>,
    failed: {} as Record<string, boolean>,
  }),
  actions: {
    /** 同步取配置：未加载/失败一律回落默认包，保证消费方首屏可用。 */
    getConfig(caseId: string): OntologyConfig {
      return (caseId && this.byCase[caseId]) || DEFAULT_ONTOLOGY_CONFIG
    },

    /** 拉取案件配置（幂等：已加载不重复拉；force 用于 BUILD 后刷新）。 */
    async ensure(caseId: string, force = false): Promise<OntologyConfig> {
      if (!caseId) return DEFAULT_ONTOLOGY_CONFIG
      if (!force && (this.byCase[caseId] || this.loading[caseId])) {
        return this.byCase[caseId] ?? DEFAULT_ONTOLOGY_CONFIG
      }
      this.loading[caseId] = true
      try {
        this.byCase[caseId] = await ontologyConfigApi.get(caseId)
        this.failed[caseId] = false
        return this.byCase[caseId]
      } catch {
        // 旧后端/离线/权限异常：回落默认配置，不阻塞工作台
        this.failed[caseId] = true
        return DEFAULT_ONTOLOGY_CONFIG
      } finally {
        this.loading[caseId] = false
      }
    },

    /** 切案件/登出清理 */
    clear(caseId?: string): void {
      if (caseId) {
        delete this.byCase[caseId]
        delete this.loading[caseId]
        delete this.failed[caseId]
      } else {
        this.byCase = {}
        this.loading = {}
        this.failed = {}
      }
    },
  },
})
