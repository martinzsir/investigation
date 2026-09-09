import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

// MVP-5 视图/组件/MSW 红线源码断言（范式同 config-redlines.spec.ts）：
// 视图层只做编排，红线判定全部在 domain 纯函数；本文件锁死「视图必须挂红线闸门」
// 与「MSW 不得 mock 成永远通过」两条纪律，防回归。

const here = dirname(fileURLToPath(import.meta.url))
const view = (p: string) => readFileSync(resolve(here, '..', 'src', 'views', p), 'utf8')
const comp = (p: string) => readFileSync(resolve(here, '..', 'src', 'components', p), 'utf8')
const handlers = () => readFileSync(resolve(here, '..', 'mocks', 'handlers.ts'), 'utf8')

describe('MVP-5 门户 PortalView', () => {
  const s = view('PortalView.vue')
  it('三态经 portalViewState 派生，空库/无结果/有数据不空判', () => {
    expect(s).toContain('portalViewState')
    expect(s).toContain('EmptyState')
  })
  it('B1：状态/搜索筛选走 filterCases 客户端（GET /cases 无查询参数）', () => {
    expect(s).toContain('filterCases')
  })
  it('建案/归档双校验：caseIdError/caseNameError + canArchive + 归档 reason 留痕', () => {
    expect(s).toContain('caseIdError')
    expect(s).toContain('caseNameError')
    expect(s).toContain('canArchive')
    expect(s).toContain('archiveReason')
  })
  it('A3：卡片汇总并发（Promise.allSettled 单卡降级，不拖垮整页）', () => {
    expect(s).toMatch(/Promise\.allSettled/)
  })
})

describe('MVP-5 跨案件 CrossCaseView（全有或全无）', () => {
  const s = view('CrossCaseView.vue')
  it('执行前必经 authorizeSelection → canExecute/executeBlockReason 拦截链', () => {
    expect(s).toContain('authorizeSelection')
    expect(s).toContain('canExecute')
    expect(s).toContain('executeBlockReason')
  })
  it('无权案件 denied 有红框态与一键移除，不放行到 SQL 执行', () => {
    expect(s).toContain('authz.denied')
    expect(s).toContain('removeDenied')
  })
  it('查询事由必填留痕（reasonError），模板只产 UNION ALL 骨架', () => {
    expect(s).toContain('reasonError')
    expect(s).toContain('buildUnionTemplate')
    // 前端不构造 ATTACH SQL（READ_ONLY ATTACH 是后端能力）、无任何写请求
    expect(s).not.toMatch(/ATTACH\s+case_/i)
    expect(s).not.toContain('api.put')
  })
  it('案件 chip 用 div 单点切换（label 包 NCheckbox 会双重 toggle），执行按钮含事由闸门', () => {
    expect(s).not.toMatch(/<label[^>]*class="case-chip"/)
    expect(s).toMatch(/class="case-chip"[\s\S]*?@click="toggleCase/)
    expect(s).toMatch(/:disabled="!executable \|\| !sqlReady \|\| !!reasonErr"/)
  })
})

describe('MVP-5 案件包 PackageView（留痕/告警/门控）', () => {
  const s = view('PackageView.vue')
  it('七步清单 StepChecklist 接 verify.steps，fail/warn 有色调', () => {
    expect(s).toContain('StepChecklist')
    // 七步排序/补位在组件内部（orderedSteps），视图只传 verify.steps
    const c = comp('tools/StepChecklist.vue')
    expect(c).toContain('orderedSteps')
    expect(c).toContain('stepTone')
  })
  it('审计链不完整显橙色告警（chainWarning），敏感文件红框（sensitiveRedList）', () => {
    expect(s).toContain('chainWarning')
    expect(s).toContain('sensitiveRedList')
  })
  it('canImport 门控：校验不通过不显示导入表单', () => {
    expect(s).toContain('canImport')
  })
  it('导出/导入走任务 SSE，终态关闭流；下载走 Blob', () => {
    expect(s).toContain('tasksApi.stream')
    expect(s).toContain('packageApi.download')
    expect(s).toMatch(/onBeforeUnmount/)
  })
})

describe('MVP-5 系统设置 SettingsView（fail-closed）', () => {
  const s = view('SettingsView.vue')
  it('非管理员直接锁定面板（canViewAdminSettings + forbidden 态），不靠 403 探测', () => {
    expect(s).toContain('canViewAdminSettings')
    expect(s).toMatch(/forbidden/)
  })
  it('health 登录常开；admin 四域编辑必经 ConfigConfirmDialog + reason', () => {
    expect(s).toContain('settingsApi.health')
    expect(s).toContain('ConfigConfirmDialog')
    expect(s).toContain('canSubmit')
  })
  it('红线键 LOCKED_REDLINES 静态展示为不可改（llm_enabled/audit_immutable）', () => {
    expect(s).toContain('LOCKED_REDLINES')
  })
})

describe('MVP-5 庙算工作台 MiaoSuanView（FE-T-014 候补不升格）', () => {
  const s = view('MiaoSuanView.vue')
  it('候补池在 candidate-zone 隔离区，受限线索灰显', () => {
    expect(s).toContain('CANDIDATE_ZONE_CLASS')
    expect(s).toContain('restrictedList')
  })
  it('视图层不出现任何升格字段词（cross_level/new_level/level_up/promoted/upgrade）', () => {
    expect(s).not.toMatch(/cross_level|new_level|level_up|promoted|upgrade/)
  })
  it('双轨对比与热力图组件挂载', () => {
    expect(s).toContain('DualTrackCompare')
    expect(s).toContain('HeatGrid')
  })
})

describe('MVP-5 知识图谱 GraphView（截断/降级）', () => {
  const s = view('GraphView.vue')
  it('截断横幅 truncatedBanner（节点采样/丢边数真实态）', () => {
    expect(s).toContain('truncatedBanner')
  })
  it('G6 动态 import 失败降级表格（@fallback），核心节点按度数侧栏', () => {
    expect(s).toContain('GraphCanvas')
    expect(s).toContain('@fallback')
    expect(s).toContain('nodesByDegree')
  })
  it('节点下钻回线索页（nodeDrillHref），不在图上写判定', () => {
    expect(s).toContain('nodeDrillHref')
  })
  it('GraphCanvas 动态加载 @antv/g6（失败可降级），卸载销毁', () => {
    const c = comp('research/GraphCanvas.vue')
    expect(c).toMatch(/await import\('@antv\/g6'\)/)
    expect(c).toContain("emit('fallback')")
    expect(c).toContain('onBeforeUnmount')
  })
})

describe('MVP-5 代码逃生舱 EscapeHatchView（只生成文本）', () => {
  const s = view('EscapeHatchView.vue')
  it('仅调 generate/stats；POST 不携幂等键（不写盘、不注册、不执行）', () => {
    expect(s).toContain('escapeHatchApi.generate')
    expect(s).toContain('escapeHatchApi.stats')
    expect(s).not.toContain('idempotencyAction')
  })
  it('名称/描述经 stubNameError/stubDescError 校验', () => {
    expect(s).toContain('stubNameError')
    expect(s).toContain('stubDescError')
  })
})

describe('MVP-5 审计链维度切换（p7 技术债）', () => {
  const s = view('AuditChainView.vue')
  it('segmented 维度切换：案件级 / 线索级（NRadioButton）', () => {
    expect(s).toContain('NRadioButton')
    expect(s).toContain('案件级')
    expect(s).toContain('线索级')
  })
  it('线索级经 ?clue_id= query 进入并回写 URL（外页可直达/可分享）', () => {
    expect(s).toContain('route.query.clue_id')
    expect(s).toContain('router.replace')
    expect(s).toContain('clue_id')
  })
})

describe('MVP-5 MSW handlers 红线纪律（不得 mock 成永远通过）', () => {
  const h = handlers()
  it('跨案查询：含无权案件整体 403（全有或全无文案真实）', () => {
    expect(h).toContain('全有或全无')
    expect(h).toMatch(/FORBIDDEN.*403/)
  })
  it('庙算候补 candidates 区块结构上无升格字段（FE-T-014 mock 侧同纪律）', () => {
    const m = h.match(/candidates: \[([\s\S]*?)\],\s*restricted/)
    expect(m).not.toBeNull()
    expect(m?.[1]).not.toMatch(/cross_level|new_level|level_up|promoted|upgrade/)
  })
  it('设置管理面：非 admin 的 GET/PUT 均 403；health 不设门槛', () => {
    const occurrences = h.match(/平台设置仅管理员可操作/g)?.length ?? 0
    expect(occurrences).toBeGreaterThanOrEqual(6)
    const healthBlock = h.match(/settings\/health'[\s\S]*?\}\),/)?.[0] ?? ''
    expect(healthBlock).not.toContain('isMockAdmin')
  })
  it('案件包校验：七步 key 齐备，且造出 chain warn / hash fail 非常态', () => {
    for (const k of ['format', 'manifest', 'hash', 'declarations', 'schema', 'chain', 'duckdb']) {
      expect(h).toContain(`key: '${k}'`)
    }
    expect(h).toMatch(/noChain|broken|无链/)
    expect(h).toMatch(/root_hash 与内容不符|篡改/)
  })
  it('下载端点返真实 zip Blob（非 JSON 信封）', () => {
    expect(h).toContain("type: 'application/zip'")
    expect(h).toContain('Content-Disposition')
  })
  it('写操作留痕：建案/归档/跨案/设置改 reason 缺失即 400', () => {
    expect(h).toMatch(/归档必须填写原因/)
    expect(h).toMatch(/查询事由必填/)
    expect(h).toMatch(/修改平台设置必须填写原因/)
  })
})
