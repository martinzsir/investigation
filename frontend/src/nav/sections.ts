// FE-C-026：侧栏六分组导航（MVP-0 全部为占位路由，标注交付里程碑）
export interface NavItem {
  key: string
  label: string
  mvp: number
}

export interface NavGroup {
  key: string
  label: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    key: 'research',
    label: '研判中心',
    items: [
      { key: 'overview', label: '治理仪表盘', mvp: 1 },
      { key: 'clues', label: '线索列表', mvp: 1 },
      { key: 'board', label: '处置看板', mvp: 2 },
      { key: 'verdict', label: '实体裁决', mvp: 2 },
      { key: 'miaosuan', label: '庙算工作台', mvp: 5 },
      { key: 'graph', label: '知识图谱', mvp: 5 },
    ],
  },
  {
    key: 'ingest',
    label: '数据接入',
    items: [
      { key: 'wizard', label: '接入向导', mvp: 3 },
      { key: 'profile', label: '数据画像', mvp: 2 },
      { key: 'suggest', label: '接入建议', mvp: 3 },
    ],
  },
  {
    key: 'gov',
    label: '数据治理',
    items: [
      { key: 'data-elements', label: '数据元', mvp: 4 },
      { key: 'etl', label: 'ETL 管道', mvp: 4 },
      { key: 'mapping', label: '映射校验', mvp: 4 },
      { key: 'quality', label: '质量检查', mvp: 4 },
      { key: 'quarantine', label: '隔离区', mvp: 4 },
    ],
  },
  {
    key: 'model',
    label: '研判模型',
    items: [
      { key: 'designer', label: '模型设计器', mvp: 4 },
      { key: 'rules', label: '规则工坊', mvp: 4 },
      { key: 'functions', label: '函数目录', mvp: 4 },
      { key: 'masking', label: '权限与遮蔽', mvp: 4 },
    ],
  },
  {
    key: 'audit',
    label: '治理审计',
    items: [{ key: 'audit-chain', label: '审计链', mvp: 1 }],
  },
  {
    key: 'tools',
    label: '高级工具',
    items: [
      { key: 'cross-case', label: '跨案件查询', mvp: 5 },
      { key: 'package', label: '案件包', mvp: 5 },
      { key: 'escape', label: '代码逃生舱', mvp: 5 },
    ],
  },
]

export function findSection(key: string): { group: NavGroup; item: NavItem } | null {
  for (const group of NAV_GROUPS) {
    const item = group.items.find((i) => i.key === key)
    if (item) return { group, item }
  }
  return null
}
