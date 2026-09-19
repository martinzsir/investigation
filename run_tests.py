"""
run_tests.py —— 统一测试入口

测试组（--only 可选）：
  mcp         MCP server 端到端（scripts.mcp_client_test）
  graph       图库层 Q2 过桥双轨（tests.test_graph）
  miaosuan    庙算假设引擎（tests.test_miaosuan）
  org         组织层级对齐（tests.test_org_alignment）
  review      人工确认工作台（tests.test_review_queue）
  disposal    处置状态机 + 审计链（test_disposal.py 脚本式）
  ontology    语义层 Object/Link/Action（tests.test_ontology）
  version     REQ-001 版本时钟/依赖图（tests.test_ontology_version）
  eventbus    REQ-006 事件总线（tests.test_event_bus）
  ingest      REQ-005 分区校验隔离（tests.test_ingest_validate）
  spec        Schema/规则/引用完整性（test_schemas/test_rule_schema/
              test_reference_integrity/test_audit_chain）
  planner     REQ-018 受影响范围计算（tests.test_rebuild_planner）
  gateway     REQ-002 语义层读网关（tests.test_gateway）
  guard       REQ-003 直查拦截 + 静态扫描（tests.test_store_guard）
  features    REQ-015 L1 特征落盘（tests.test_features）
  incremental REQ-004 语义层增量重建（tests.test_incremental_semantic）
  audit       REQ-003 直查静态扫描（scripts/audit_straight_sql.py）
  access      REQ-009 AccessContext 权限上下文（tests.test_access）
  policy      REQ-010 对象级/属性级策略（tests.test_policy）
  export      REQ-011 导出权限与审计（tests.test_export_policy）
  action      REQ-012 Action 两阶段提交（tests.test_action_two_phase）
  writeback   REQ-013/043 回写适配器+发件箱+Console（tests.test_writeback + tests.test_writeback_console）
  reconcile   REQ-014 对账重试死信（tests.test_reconcile）
  reviewloop  REQ-016 review 闭环增量重建（tests.test_review_loop）
  deferred    REQ-017 defer 回捞（tests.test_deferred）
  r5knowledge REQ-024 R5 知识包参数化（tests.test_rule_r5_knowledge）
  golden      REQ-020 Golden Finding 回归（tests.test_golden）
  overlap     REQ-025 规则互斥与重叠消解（tests.test_rule_overlap）
  threshold   REQ-027 阈值策略对象（tests.test_threshold_adaptive）
  derived     REQ-028/R8 DerivedProperty 查询时派生+端点（tests.test_derived、tests.test_derived_api）
  object_set  REQ-029 ObjectSet 查询构造器（tests.test_object_set）
  metrics     REQ-030 规则运行时度量（tests.test_metrics）
  rule_dsl    REQ-026 规则 DSL 组合与时序（tests.test_rule_dsl）
  llmpolicy   REQ-038 LLM 策略与脱敏闸门（tests.test_llm_policy）
  proposal    REQ-033 ProposalStore 强类型提案（tests.test_proposal）
  injection   REQ-039 提示注入防护（tests.test_injection）
  caselib     REQ-031 案例库片段沉淀（tests.test_case_library）
  params      REQ-032 参数治理版本/审批/回滚（tests.test_parameters）
  pack        REQ-044 多案件包与隔离（tests.test_pack）
  views       REQ-046 Object Views 按角色投影（tests.test_views）
  benchmarks  REQ-045 性能基准（tests.test_benchmarks）
  e2e         端到端集成（tests.test_run_all）

用法：
    python run_tests.py              # 跑全部
    python run_tests.py --fast       # 跳过端到端（最快反馈）
    python run_tests.py --only org   # 只跑指定组
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

GROUPS = {
    "mcp":         ("MCP Server 端到端", [sys.executable, "-m", "scripts.mcp_client_test"]),
    "graph":       ("图库层 Q2 过桥双轨", [sys.executable, "-m", "unittest", "tests.test_graph"]),
    "miaosuan":    ("庙算假设引擎 三层机制", [sys.executable, "-m", "unittest", "tests.test_miaosuan"]),
    "org":         ("组织层级对齐", [sys.executable, "-m", "unittest", "tests.test_org_alignment"]),
    "review":      ("人工确认工作台", [sys.executable, "-m", "unittest", "tests.test_review_queue"]),
    "disposal":    ("处置状态机+审计链", [sys.executable, "test_disposal.py"]),
    "ontology":    ("语义层 Object/Link/Action", [sys.executable, "-m", "unittest", "tests.test_ontology"]),
    "version":     ("REQ-001 版本时钟/依赖图", [sys.executable, "-m", "unittest", "tests.test_ontology_version"]),
    "eventbus":    ("REQ-006 事件总线", [sys.executable, "-m", "unittest", "tests.test_event_bus"]),
    "ingest":      ("REQ-005 分区校验隔离", [sys.executable, "-m", "unittest", "tests.test_ingest_validate"]),
    "spec":        ("Schema/规则/引用完整性", [sys.executable, "-m", "unittest",
                                                 "tests.test_schemas", "tests.test_rule_schema",
                                                 "tests.test_reference_integrity", "tests.test_audit_chain"]),
    "planner":     ("REQ-018 受影响范围计算", [sys.executable, "-m", "unittest", "tests.test_rebuild_planner"]),
    "gateway":     ("REQ-002 语义层读网关", [sys.executable, "-m", "unittest", "tests.test_gateway"]),
    "guard":       ("REQ-003 直查拦截+静态扫描", [sys.executable, "-m", "unittest", "tests.test_store_guard"]),
    "features":    ("REQ-015 L1 特征落盘", [sys.executable, "-m", "unittest", "tests.test_features"]),
    "incremental": ("REQ-004 语义层增量重建", [sys.executable, "-m", "unittest", "tests.test_incremental_semantic"]),
    "rowuri":      ("REQ-008 内容寻址溯源行 URI", [sys.executable, "-m", "unittest", "tests.test_row_uri"]),
    "audit":       ("REQ-003 直查静态扫描", [sys.executable, "scripts/audit_straight_sql.py", "--fail-on-violation"]),
    "access":      ("REQ-009 AccessContext 权限上下文", [sys.executable, "-m", "unittest", "tests.test_access"]),
    "policy":      ("REQ-010 对象级/属性级策略", [sys.executable, "-m", "unittest", "tests.test_policy"]),
    "export":      ("REQ-011 导出权限与审计", [sys.executable, "-m", "unittest", "tests.test_export_policy"]),
    "action":      ("REQ-012 Action 两阶段提交", [sys.executable, "-m", "unittest", "tests.test_action_two_phase"]),
    "writeback":   ("REQ-013/043 回写适配器+发件箱+Console", [sys.executable, "-m", "unittest", "tests.test_writeback", "tests.test_writeback_console"]),
    "reconcile":   ("REQ-014 对账重试死信", [sys.executable, "-m", "unittest", "tests.test_reconcile"]),
    "reviewloop":  ("REQ-016 review 闭环增量重建", [sys.executable, "-m", "unittest", "tests.test_review_loop"]),
    "deferred":    ("REQ-017 defer 回捞", [sys.executable, "-m", "unittest", "tests.test_deferred"]),
    "r5knowledge": ("REQ-024 R5 知识包参数化", [sys.executable, "-m", "unittest", "tests.test_rule_r5_knowledge"]),
    "golden":      ("REQ-020 Golden Finding 回归", [sys.executable, "-m", "unittest", "tests.test_golden"]),
    "overlap":     ("REQ-025 规则互斥与重叠消解", [sys.executable, "-m", "unittest", "tests.test_rule_overlap"]),
    "threshold":   ("REQ-027 阈值策略对象", [sys.executable, "-m", "unittest", "tests.test_threshold_adaptive"]),
    "derived":     ("REQ-028/R8 DerivedProperty 查询时派生+端点", [sys.executable, "-m", "unittest", "tests.test_derived", "tests.test_derived_api"]),
    "object_set":  ("REQ-029 ObjectSet 查询构造器", [sys.executable, "-m", "unittest", "tests.test_object_set"]),
    "metrics":     ("REQ-030 规则运行时度量", [sys.executable, "-m", "unittest", "tests.test_metrics"]),
    "rule_dsl":    ("REQ-026 规则 DSL 组合与时序", [sys.executable, "-m", "unittest", "tests.test_rule_dsl"]),
    "llmpolicy":   ("REQ-038 LLM 策略与脱敏闸门", [sys.executable, "-m", "unittest", "tests.test_llm_policy"]),
    "llmfallback": ("REQ-040 LLM 降级开关与影子模式", [sys.executable, "-m", "unittest", "tests.test_llm_fallback"]),
    "proposal":    ("REQ-033 ProposalStore 强类型提案", [sys.executable, "-m", "unittest", "tests.test_proposal"]),
    "reviewwrite": ("REQ-021-write Agent 提案写轨", [sys.executable, "-m", "unittest", "tests.test_review_write"]),
    "injection":   ("REQ-039 提示注入防护", [sys.executable, "-m", "unittest", "tests.test_injection"]),
    "caselib":     ("REQ-031 案例库片段沉淀", [sys.executable, "-m", "unittest", "tests.test_case_library"]),
    "params":      ("REQ-032 参数治理版本/审批/回滚", [sys.executable, "-m", "unittest", "tests.test_parameters"]),
    "search":      ("REQ-042 语义检索（受控）", [sys.executable, "-m", "unittest", "tests.test_semantic_search"]),
    "types":       ("REQ-041 类型系统扩展", [sys.executable, "-m", "unittest", "tests.test_type_extension"]),
    "pack":        ("REQ-044 多案件包与隔离", [sys.executable, "-m", "unittest", "tests.test_pack"]),
    "views":       ("REQ-046 Object Views 按角色投影", [sys.executable, "-m", "unittest", "tests.test_views"]),
    "benchmarks":  ("REQ-045 性能基准", [sys.executable, "-m", "unittest", "tests.test_benchmarks"]),
    "llm":         ("REQ-034~037 LLM 草案/解释/对齐/意图", [sys.executable, "-m", "unittest", "tests.test_llm_draft"]),
    # ---- REQ-G 统一降级协议（第一波：底座留痕）----
    "runhealth":   ("REQ-G-010 运行诊断/健康度层", [sys.executable, "-m", "unittest", "tests.test_run_health"]),
    "rulezerodiag":("REQ-G-002 规则零命中诊断", [sys.executable, "-m", "unittest", "tests.test_rule_zero_diag"]),
    "emptydegrade":("REQ-G-003 函数空转/结构降级", [sys.executable, "-m", "unittest", "tests.test_empty_degrade"]),
    "eventtrace":  ("REQ-G-004 事件发布留痕", [sys.executable, "-m", "unittest", "tests.test_event_trace"]),
    "wakecond":    ("REQ-G-005 唤醒条件不可解析", [sys.executable, "-m", "unittest", "tests.test_wake_condition"]),
    "entitytrace": ("REQ-G-006 实体解析跳过/插件留痕", [sys.executable, "-m", "unittest", "tests.test_entity_trace"]),
    "entityredline":("红线 R-1 同名实体强证据分区", [sys.executable, "-m", "unittest", "tests.test_entity_redline"]),
    "dirtydate":    ("脏日期 TRY_CAST 降级（鲁棒性 B2-08）", [sys.executable, "-m", "unittest", "tests.test_dirty_date"]),
    "misscol":      ("缺列预检：可选降级/必填硬失败（鲁棒性 B5-01/03）", [sys.executable, "-m", "unittest", "tests.test_missing_column"]),
    "one2one":      ("REQ-D-012 1:1 约束守护（一源列一属性）", [sys.executable, "-m", "unittest", "tests.test_one2one"]),
    "opregistry":   ("REQ-D-004 清洗规则注册表（唯一 op 注册表）", [sys.executable, "-m", "unittest", "tests.test_op_registry"]),
    "dataelements": ("REQ-D-001 数据元标准注册（第 14 声明文件）", [sys.executable, "-m", "unittest", "tests.test_data_elements"]),
    "cleanscope":   ("REQ-D-005 属性级清洗作用域（双通道）", [sys.executable, "-m", "unittest", "tests.test_clean_scope"]),
    "transform":    ("REQ-D-009 transform 层（脏值可用性抢救）", [sys.executable, "-m", "unittest", "tests.test_transform"]),
    "casterror":    ("REQ-D-010 on_cast_error 三态（null/fail/quarantine）", [sys.executable, "-m", "unittest", "tests.test_cast_error"]),
    "nullidentity": ("鲁棒性：NULL 名实体剔除留痕 + 整数金额 TRY_CAST 防 Inf 崩溃", [sys.executable, "-m", "unittest", "tests.test_null_identity"]),
    "composite":    ("REQ-D-013 复合列声明（显式降级+画像检出）", [sys.executable, "-m", "unittest", "tests.test_composite"]),
    "reqdcase":     ("REQ-D 海州电诈案 24 探针端到端验收", [sys.executable, "-m", "unittest", "tests.test_reqd_case"]),
    "compliance":   ("REQ-D-016 数据元合规扫描（违规行落诊断+画像违规率）", [sys.executable, "-m", "unittest", "tests.test_compliance"]),
    "deingest":     ("数据元驱动接入全链路（人员信息对象/transform抢救/py清洗/合规/遮蔽/五格式夹具）", [sys.executable, "-m", "unittest", "tests.test_de_ingest"]),
    "cleanstats":   ("REQ-D-008 清洗统计落健康度（剔除率告警+画像前后行数）", [sys.executable, "-m", "unittest", "tests.test_clean_stats"]),
    "sensitivescan":("REQ-D-018 敏感列启发式扫描（只告警不阻断）", [sys.executable, "-m", "unittest", "tests.test_sensitive_scan"]),
    "versionanchor":("REQ-G-007/001 版本锚定+缓存失效令牌", [sys.executable, "-m", "unittest", "tests.test_version_anchor", "tests.test_derived"]),
    # ---- REQ-G 统一降级协议（第二波：治理口径）----
    "policyversion":("REQ-G-016 policies/case_knowledge 版本收口", [sys.executable, "-m", "unittest", "tests.test_policy_version"]),
    "dimecoverage": ("REQ-G-008/009/024 维度覆盖双轨+阈值边界+实证缺口独立报警", [sys.executable, "-m", "unittest", "tests.test_miaosuan"]),
    "overridealert":("REQ-G-017 规则推翻率告警", [sys.executable, "-m", "unittest", "tests.test_override_alert"]),
    "auditinteg":   ("REQ-G-018 审计链完备性自检", [sys.executable, "-m", "unittest", "tests.test_audit_integrity"]),
    "dispatchfailclosed":("REQ-G-020 派发 fail-closed", [sys.executable, "-m", "unittest", "tests.test_dispatch_failclosed"]),
    # ---- REQ-G 统一降级协议（第三波：声明化）----
    "declconfig":  ("REQ-G-011/012/013 维度/枚举/间类声明化", [sys.executable, "-m", "unittest", "tests.test_decl_config"]),
    "anomalychannel":("REQ-G-019 异常线索通道（不参与交叉）", [sys.executable, "-m", "unittest", "tests.test_anomaly_channel"]),
    "geo":         ("REQ-G-021 地点标准化/同框 Function", [sys.executable, "-m", "unittest", "tests.test_geo"]),
    "initcold":    ("REQ-G-014 冷层建表声明推导", [sys.executable, "-m", "unittest", "tests.test_init_cold"]),
    "exportendpoints":("REQ-G-015 端点列名声明化/导出通用化", [sys.executable, "-m", "unittest", "tests.test_export_endpoints"]),
    "reqpm1":      ("REQ-P M1 数据层缺陷修复（031~034）", [sys.executable, "-m", "unittest", "tests.test_reqp_m1"]),
    "datamap":     ("REQ-P M2 数据地图 L0+L1 静态拓扑与血缘", [sys.executable, "-m", "unittest", "tests.test_data_map"]),
    "valuetype":   ("REQ-P M3 值类型识别（纯函数，缺陷1/2内置）", [sys.executable, "-m", "unittest", "tests.test_value_type"]),
    "profiler":    ("REQ-P M4 六层本体画像（L1/L2/L3/L4/L5）", [sys.executable, "-m", "unittest", "tests.test_ontology_profiler"]),
    "drafts":      ("REQ-P M6 新表画像+草案组装器+步骤推荐", [sys.executable, "-m", "unittest", "tests.test_draft_assembler"]),
    # ---- REQ-D 批 D5（贯通汇聚，8 需求 7 组）----
    "compliance_score": ("REQ-D-017 合规违规率进画像五要素扣分", [sys.executable, "-m", "unittest", "tests.test_compliance_score"]),
    "de_ref":      ("REQ-D-002 属性引用数据元 AD-5 展开继承", [sys.executable, "-m", "unittest", "tests.test_de_ref"]),
    "health_integration": ("REQ-D-022 四类结果统一进 RunHealth + by_source", [sys.executable, "-m", "unittest", "tests.test_health_integration"]),
    "declarative_ops": ("REQ-D-006/011 声明式带参 op + 脏格式归一", [sys.executable, "-m", "unittest", "tests.test_declarative_ops"]),
    "wordlist":    ("REQ-D-007 清洗词表外置 clean_rules.json", [sys.executable, "-m", "unittest", "tests.test_wordlist"]),
    "codetable":   ("REQ-D-003 代码表数据元派生/枚举去人名/快照锁定", [sys.executable, "-m", "unittest", "tests.test_codetable"]),
    "recommend":   ("REQ-D-021 数据元驱动落点推荐（只进 draft）", [sys.executable, "-m", "unittest", "tests.test_recommend"]),
    # ---- REQ-D 批 D6（P2 增强，4 需求 4 组）----
    "null_policy":    ("REQ-D-014 属性级空值策略 allow/reject/quarantine（空值先于 CAST）", [sys.executable, "-m", "unittest", "tests.test_null_policy"]),
    "dedup_key":      ("REQ-D-015 业务键去重 keep_latest/keep_first/fail", [sys.executable, "-m", "unittest", "tests.test_dedup_key"]),
    "data_freshness": ("REQ-D-019 数据时间新鲜度（与本体版本新鲜度分开）", [sys.executable, "-m", "unittest", "tests.test_data_freshness"]),
    "unit_consistency": ("REQ-D-020 单位/口径一致性扫描（元/万元混用提示）", [sys.executable, "-m", "unittest", "tests.test_unit_consistency"]),
    "e2e":         ("端到端集成", [sys.executable, "-m", "unittest", "tests.test_run_all"]),
    # ---- Web M1（server/ 服务端地基）----
    "storeback":   ("M1 阶段A StoreBackend 抽象+服务端开库门禁", [sys.executable, "-m", "unittest", "tests.test_store_backend"]),
    "metastore":   ("M1 阶段B 元数据层 SQLite-WAL+状态机+幂等", [sys.executable, "-m", "unittest", "tests.test_meta_store"]),
    "casesnap":    ("M1 阶段C 案件生命周期+pack快照锁定", [sys.executable, "-m", "unittest", "tests.test_case_snapshot"]),
    "taskqueue":   ("M1 阶段D/E 版本原子切换+任务队列Worker", [sys.executable, "-m", "unittest", "tests.test_task_queue"]),
    "apibase":     ("M1 阶段F FastAPI骨架+S1~S6红线", [sys.executable, "-m", "unittest", "tests.test_api_base"]),
    # ---- Web M2（可信首页：回收/孤儿/审计/仪表盘/state 骨架）----
    "reclaim":     ("M2 W-008 版本延迟回收+读者租约+归档压实", [sys.executable, "-m", "unittest", "tests.test_version_reclaim"]),
    "orphanscan":  ("M2 W-009 孤儿版本扫描+隔离区TTL", [sys.executable, "-m", "unittest", "tests.test_orphan_scan"]),
    "auditview":   ("M2 W-023/024 审计时间线/自检空链红线/平台审计", [sys.executable, "-m", "unittest", "tests.test_audit_view"]),
    "dashboard":   ("M2 W-018 治理仪表盘组装+诊断下钻", [sys.executable, "-m", "unittest", "tests.test_dashboard"]),
    "statestore":  ("M2 D1 state.sqlite 骨架+幂等迁移演练", [sys.executable, "-m", "unittest", "tests.test_state_store"]),
    # ---- Web M3（研判主流程：state 接线/处置/线索/规则工坊/接入）----
    "statesink":   ("M3 D1 写路径接线 StateSink+五动作 sqlite 后端+红线", [sys.executable, "-m", "unittest", "tests.test_state_sink"]),
    "disposeapi":  ("M3 W-020 处置动作 DISPOSE 快速通道+状态机红线", [sys.executable, "-m", "unittest", "tests.test_dispose_api"]),
    "cluesread":   ("M3 W-019 线索读面列表/详情/suppressed+state 真值", [sys.executable, "-m", "unittest", "tests.test_clues_read"]),
    "ruleworkshop":("M3 W-014/025 规则工坊读写+RESCAN+LLM 守卫", [sys.executable, "-m", "unittest", "tests.test_rule_workshop"]),
    "ingestapi":   ("M3 W-010/012 数据接入五格式上传+幂等+冷层导入", [sys.executable, "-m", "unittest", "tests.test_ingest_api"]),
    "m3chain":     ("M3 阶段F 研判主流程全链路冒烟（接入→BUILD→线索→处置→审计→RESCAN）", [sys.executable, "-m", "unittest", "tests.test_m3_chain"]),
    "modeldesigner":("M4 W-013 对象模型设计器 objects/links/validate", [sys.executable, "-m", "unittest", "tests.test_model_designer"]),
    "accessconfig": ("M4 W-015 权限与字段遮蔽配置", [sys.executable, "-m", "unittest", "tests.test_access_config"]),
    "knowledgeapi": ("M4 W-016 知识包维护", [sys.executable, "-m", "unittest", "tests.test_knowledge_api"]),
    "viewsapi":     ("M4 W-017 角色视图配置", [sys.executable, "-m", "unittest", "tests.test_views_api"]),
    "anomalyapi":   ("M4 W-022 异常线索通道", [sys.executable, "-m", "unittest", "tests.test_anomaly_api"]),
    "datagov":      ("M4 W-011 数据治理缺列降级", [sys.executable, "-m", "unittest", "tests.test_data_governance"]),
    "reviewapi":    ("M4 W-021 人审队列与实体裁决", [sys.executable, "-m", "unittest", "tests.test_review_api"]),
    "crosscase":    ("M5 W-026/027 跨案件查询与全有或全无鉴权", [sys.executable, "-m", "unittest", "tests.test_cross_case_api"]),
    "packageapi":   ("M5 W-028/029 案件包导出导入", [sys.executable, "-m", "unittest", "tests.test_package_api"]),
    "escapehatch":  ("M5 W-031 代码逃生舱代码桩生成", [sys.executable, "-m", "unittest", "tests.test_escape_hatch_api"]),
    # ---- Web M6（前端配套端点：看板/向导/图谱）----
    "m6board":      ("M6 W-P-001~004 处置看板+列分析+映射草稿+关系图谱", [sys.executable, "-m", "unittest", "tests.test_m6_board"]),
    "m6insight":    ("M6 W-P-005~010 庙算+画像+推荐+质检+隔离+留痕", [sys.executable, "-m", "unittest", "tests.test_m6_insight"]),
    "m6govern":     ("M6 W-P-011~015/017 数据元+ETL管道+系统设置+任务取消+门户归档+配置中心", [sys.executable, "-m", "unittest", "tests.test_m6_govern"]),
    "diagnose":     ("手动运行诊断 DIAGNOSE 任务（run_diagnostic 留痕，不随 BUILD 自动）", [sys.executable, "-m", "unittest", "tests.test_diagnose_worker"]),
    "configaudit":  ("MVP-4 FE-T-012 配置写追加案件审计链（9 端点+reason+链校验）", [sys.executable, "-m", "unittest", "tests.test_config_audit_api"]),
    "sourcerow":   ("B1 溯源行适配器（source_row_dto 字段表+遮蔽+hit）", [sys.executable, "-m", "unittest", "tests.test_source_row_dto"]),
    "evidence":    ("B2 证据三栏产出器（evidence_builder fact/inference/pending）", [sys.executable, "-m", "unittest", "tests.test_evidence_builder"]),
    "lifecycle":  ("B5 生命周期事件补录（case_created/source_imported/build_succeeded）", [sys.executable, "-m", "unittest", "tests.test_lifecycle_audit"]),
    # ---- 六项解耦（运行时地基 + 声明化）----
    "rtcontext":  ("R1/R2 RuntimeContext+ReadOnlyStore+load_pack 缓存", [sys.executable, "-m", "unittest", "tests.test_runtime_context"]),
    "fnrequires": ("R3/R4 FunctionSpec.requires 声明+py 函数表名参数化", [sys.executable, "-m", "unittest", "tests.test_function_requires"]),
    "jiansdecl":  ("P6 五间词汇 packs/wujian 装载校验", [sys.executable, "-m", "unittest", "tests.test_jians_decl"]),
    "statesdecl": ("R6/R10 状态机声明化 states.json", [sys.executable, "-m", "unittest", "tests.test_states_decl"]),
    "scoringdecl":("R7/R12/R13 计分声明化 scoring.json+score_basis", [sys.executable, "-m", "unittest", "tests.test_scoring_decl"]),
    "scoringv3":  ("P1-3/P0-1/P1-0c 计分 v3（对数曲线+等级主序+0 间类兜底）", [sys.executable, "-m", "unittest", "tests.test_scoring_v3"]),
    "pollution":  ("包缓存污染防护（多源 UNION 裁剪/缺列降级不写回 binding）", [sys.executable, "-m", "unittest", "tests.test_spec_pollution"]),
    "s0_industry": ("S0-1 行业叠加层（三层合并/override/建案拷贝/temp 补拷）", [sys.executable, "-m", "unittest", "tests.test_s0_industry"]),
    "s0_admin":   ("S0-2 本体管理员能力位（五件套/双条件门禁/授权端点）", [sys.executable, "-m", "unittest", "tests.test_s0_admin"]),
    "s0_version": ("S0-3 版本机制（指纹扩容/重算/版本历史只追加/归档）", [sys.executable, "-m", "unittest", "tests.test_s0_version"]),
    "s1_overview": ("S1 本体管理器总览 API（19 项七字段/静态映射/三层标注/局部降级）", [sys.executable, "-m", "unittest", "tests.test_s1_overview"]),
    "s3_modeling": ("S3 数据元与规则建模（函数只读目录/规则 jian_types+params 置空/数据元枚举引用/全域行业层写门禁）", [sys.executable, "-m", "unittest", "tests.test_s3_elements_rules"]),
    "s4_governance": ("S4 治理建模（Action Type 编辑器危险字段理由门禁/状态机终态保护+引用删除拦截/R5 悬空引用/loader 兜底/llm_policy 不开放）", [sys.executable, "-m", "unittest", "tests.test_s4_governance"]),
    "s5_impact":   ("S5 通用表单+影响面+轻量提案（11 schema 覆盖/五类影响面/已固证/不确定/D10 部分失败/草稿→影响面→人工发布门禁/F5 未知字段保留）", [sys.executable, "-m", "unittest", "tests.test_s5_impact"]),
    "etldraft":     ("v1.3 Phase 4 ETL 处置草稿三分类+预演+双表分离", [sys.executable, "-m", "unittest", "tests.test_etl_drafts"]),
    "verifyitem":   ("REQ-V-001~004 核查工作区 state 三表/CRUD/稳定键/状态机/Worker 写通道", [sys.executable, "-m", "unittest", "tests.test_verify_item"]),
    "verifyapi":    ("REQ-V-005 核查项 API（读同步/写 202 入队/幂等/跨租户）+ REQ-V-008 固证/排除核查门禁", [sys.executable, "-m", "unittest", "tests.test_verify_api"]),
    "verifysuggest": ("REQ-V-018 核查手册建议项（playbook 装载硬失败/确定性渲染/采纳路由/agent 拒绝）", [sys.executable, "-m", "unittest", "tests.test_verify_suggest"]),
    "evidencefile": ("REQ-V-009 证据材料存储（文件名消毒/sha256/20MB 上限/元数据 CRUD/挂接）", [sys.executable, "-m", "unittest", "tests.test_evidence_file"]),
    "verifyreq":    ("REQ-V-012 调取清单台账（verify_request CRUD/状态机/超期读面派生）", [sys.executable, "-m", "unittest", "tests.test_verify_request"]),
    "verifydraft":  ("REQ-V-019 LLM 核查方向草案（能力闸/端点闸/脱敏/护栏/幂等/影子/审批桥接）", [sys.executable, "-m", "unittest", "tests.test_verify_draft"]),
    "verifyreplay": ("REQ-V-016 核查方向→只读 Function 映射（playbook 唯一数据源/关键词兜底/fail-closed）+ REQ-V-017 一键复跑回填（replay_json 列/op=replay/审计留痕）", [sys.executable, "-m", "unittest", "tests.test_verify_replay"]),
    "canvas":       ("线索研判画布 M1/M2/M3/M4/M5/M6（RC-101 种子成图/RC-205 自动保存/RC-207 降级/RC-102 规则双视图/RC-103 逐层溯源/RC-104 字段遮蔽/RC-201 钉住布局/RC-202 人工节点/RC-203 连线矩阵/RC-206 快照回滚/RC-105 手册建议采纳与待核实生成/RC-204 白名单 Function 扩展查询/RC-302 引用强制校验/RC-301 画布只读问答/RC-304 报告生成/RC-305 报告阅读/RC-306 MD/Word 导出）", [sys.executable, "-m", "unittest", "tests.test_canvas_seed", "tests.test_canvas_expand", "tests.test_canvas_edit", "tests.test_canvas_infer", "tests.test_canvas_api", "tests.test_canvas_citation_guard", "tests.test_canvas_chat", "tests.test_canvas_report"]),
    "sunzireport":  ("sunzi-report skill 十段报告（第九段数据源清单确定性采集/渲染，sections 越界忽略）", [sys.executable, "-m", "unittest", "tests.test_sunzi_report_skill"]),
    # ---- P3 镜头契约/失败隔离/自枚举 ----
    "skillisolation": ("P3 镜头失败隔离/enabled 短路/params 核对/evidence_refs 契约/作用域", [sys.executable, "-m", "unittest", "tests.test_skill_isolation"]),
    "packenum":     ("P3 镜头包自枚举（packs/* 挂载/拔出/坏包不连坐/内置引导）", [sys.executable, "-m", "unittest", "tests.test_pack_enum"]),
    "relationfn":   ("P4 关系研判 Function（N跳邻域/共同邻居/路径枚举，语义层统一图+结构降级）", [sys.executable, "-m", "unittest", "tests.test_relation_functions"]),
    "relationpack": ("P4 关系镜头包 packs/relation（自枚举挂载/线索化/证据可校验/作用域）", [sys.executable, "-m", "unittest", "tests.test_relation_pack"]),
    # ---- P5 时间研判镜头（统一时间轴，第二个确定性样本） ----
    "timelinefn":   ("P5 时间研判 Function（事件序列邻接/周期节奏聚集/开标前后跨类型时间窗碰撞，结构降级）", [sys.executable, "-m", "unittest", "tests.test_timeline_functions"]),
    "timelinepack": ("P5 时间镜头包 packs/timeline（自枚举挂载/线索化/time_window+aggregate 证据可校验/作用域）", [sys.executable, "-m", "unittest", "tests.test_timeline_pack"]),
    "wujianopt":   ("P6 验收① 无五间包底座独立装载（关系/时间镜头可用/消费点降级）", [sys.executable, "-m", "unittest", "tests.test_wujian_optional"]),
    # ---- P8 多模态图像研判（VLM 草案通道，人验闭环） ----
    "vlmpack":     ("P8 多模态图像研判（纯字节 EXIF 剥离/敏感类别 block/草案不落生产/降级有痕/TTL stale/人验后入图入报告）", [sys.executable, "-m", "unittest", "tests.test_vlm_pack"]),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过端到端集成测试")
    ap.add_argument("--only", choices=list(GROUPS), help="只跑指定测试组")
    args = ap.parse_args()

    names = [args.only] if args.only else list(GROUPS)
    if args.fast:
        names = [n for n in names if n != "e2e"]

    failed: list[str] = []
    for name in names:
        title, cmd = GROUPS[name]
        print(f"\n{'=' * 60}\n>>> [{name}] {title}\n{'=' * 60}")
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tail = (r.stdout or "") + (r.stderr or "")
        # 只打印摘要行，避免刷屏
        for line in tail.splitlines():
            if line.startswith(("Ran ", "OK", "FAILED", "✅", "❌")) or "Error" in line:
                print("   ", line)
        if r.returncode != 0:
            failed.append(name)
            print("    --- 完整输出 ---")
            print(tail)

    print(f"\n{'=' * 60}")
    if failed:
        print(f"❌ 失败组：{failed}")
        return 1
    print(f"✅ 全部通过：{', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
