# REQ-G 治理改进（统一降级协议 · 21 项）实施计划

> 配套文档：`.trae/documents/REQ-G/REQ-G.md`（需求清单，已含 2026-09-05 核验修正）。
> 本计划只做**失败可见性 / 实证口径 / 声明化**，不放宽任何硬失败、不改交叉等级规则。
> 编号 REQ-G-001~021 固定；REQ-G-001 经核验由 P0 降 P1（非串号），REQ-G-018 为在既有 `chain_verify` 上扩展。

## Repository Research

**已就位的基础设施（复用，不重建）**：
- **事件死信表已存在**：`core/event_bus.py` 的 `event_dead_letter` 表（DDL L76-82）已在 `_dispatch`/`_dispatch_with_key` 捕获 handler 异常并落死信（L160-166、L229-235）。REQ-G-004 的真实缺口**不是没有死信**，而是 ① 调用方 `try/except: pass` 把 **publish 落盘本身失败**吞了（事件根本没进 event_log，谈何死信）；② 死信表从未被汇总进报告。
- **审计哈希链校验已存在**：`core/audit.py:chain_verify()`（L154-185）已逐条重算 signature + 校验 prev_hash 衔接。REQ-G-018 缺的是记录数期望核对、必填字段完备性、结论出口。
- **语义表缺失降级已存在**：`core/functions.py` 的 `_is_missing_semantic_table`（L37-39）+ invoke 的 CatalogException 分支（L468-478）返回 `degraded`。REQ-G-003 要把判据从"表不存在"扩到"表在但输入非零、输出为零"。
- **运行时建表入口**：`core/ontology.py:ensure_runtime_tables()`（L356）是 Action 副作用建表唯一入口（obj_decision/lnk_decision_for），编译器跳过、重建语义层不丢。`run_diagnostic` 照此模式建表。
- **产物组装点**：`run_all.py` 在 L229-238 组装 `report` dict 并 `json.dump` 到 output；健康度小节在此插入，置于结论字段之前。

**当前 gaps（逐行已核对）**：
- `core/rules.py:run_rules()`（L87-136）：`out = fx.invoke(...)` 后 `if not hit: continue`（L118-119）——零命中无 scan_rows/matched_rows/zero_type、无诊断、不计数。
- `core/functions.py`：表在但返回 0 行走正常返回，无 degraded；`_JIAN_MAP`（L228）硬编码表名→间类映射。
- `core/deferred.py`：`match_wake` 的 `int()` 转换 `except (TypeError, ValueError): pass`（L110-116）；defer/wake 两处 publish `except Exception: pass`（L154-155、L188-189）；任务状态无"条件不可解析"态。
- `core/entity.py`：缺表/缺列 `continue`（L382-386，另有 L150/223/335/346）；插件 `exec_module` 失败 `except Exception: continue`（L576-579）、entity_resolution `ImportError` 回退（L585）——均无留痕。
- `core/audit.py`（L112）、`core/case_library.py`（L143）、`core/derived.py`（L96/101）：取版本失败静默回退 `"unknown"`/`0`，无锚定失败计数。
- `core/derived.py:_source_version_set`（L90-102）：版本失败 `"unknown"` + 行数失败 `0` → 失效令牌恒为 `unknown::0`，异常期间 `until_source_change` 永不失效重算（非串号：缓存字典键 `params_hash` 已含 `obj_pks`，L124-126，跨对象隔离成立）。
- `core/action_executor.py`：dispatch 先置 `dispatching`（L213），入 outbox 包 try，`except ImportError: result["status"]="dispatching"`（L230-231）——派发失败被"进行中"掩盖；`_publish` 的 `except Exception: pass`（L291-292）。
- `core/hypotheses.py`：`DIMENSIONS`（L67）类常量；`DIMENSION_ALARM=0.8`（L71），判据 `score < 0.8` 严格小于（L255）、alarm_text 不枚举缺口（L256-257，L250 已算出 missing 未用）；`dimension_coverage`（L247-257）只统计假设声明维度；`ENUM_SPACE`（L119-125）写死本案"张卫国/李志强/A建材"。
- `core/metrics.py:override_rate`（L150）：只计算无告警，无阈值消费方。
- `scripts/init_duckdb.py`：冷层表清单硬编码（L30 `["银行流水","通话记录","招投标档案","工商信息","轨迹出行"]`），与 bindings/objects 重复定义。
- `ontology/default/links.json`：各 link 仅 `from_obj/to_obj/properties`，无端点输出列名声明（L3 注释自承"端点列名以 build_sql 输出为准"）；`scripts/export_ladybug.py` 边 SQL 逐条定制。
- 版本号不一致：`ontology/default/policies.json` schema_version=1、`ontology/default/case_knowledge.json` 无 schema_version 字段，其余声明文件均为 2。
- 同框 link `ontology/default/bindings.json`（L108）：`ON t1.location = t2.location` 字符串精确相等，无标准化/地理编码。
- 无异常线索通道：现有"待核实"是正常线索的状态锁（`core/rules.py` L124 所有 finding 恒为"待核实"），无把零命中/匹配失败/覆盖缺口转为同构、不参与交叉的平行通道。

## Files and Modules

| 文件 | 改动类型 | 对应 REQ-G |
|---|---|---|
| `core/run_health.py` | 新 | G-010 及全部留痕项的统一出口：RunHealth/NullRunHealth + run_diagnostic 表 + summary/health_section |
| `ontology/default/functions.json` | 改 | G-002：function 增可选 `diagnostics.input_tables` / `config_missing_flag` 声明 |
| `core/ontology_loader.py` | 改 | G-002（校验 diagnostics 表名是已声明对象）、G-011（dimensions.json 装载）、G-013（jian 字段枚举校验）、G-015（links endpoints 校验）、G-016（policies/case_knowledge 版本收口） |
| `core/rules.py` | 改 | G-002：零命中产诊断对象（scan/matched/zero_type），不进 findings；G-019：异常线索生成时打 is_anomaly/needs_human_review |
| `core/functions.py` | 改 | G-003：空转降级（输入非零输出为零→degraded）；G-013：五间交叉遍历 pack 声明替代 `_JIAN_MAP`；G-019：交叉计算过滤 is_anomaly |
| `core/event_bus.py` | 改 | G-004：publish 落盘失败上抛/可记录（不再被调用方吞）；提供 dead_letter 计数查询 |
| `core/action_executor.py` | 改 | G-004（_publish 失败留痕）、G-020（dispatch 失败置 dispatch_failed + critical，不再停留 dispatching） |
| `core/deferred.py` | 改 | G-004（publish 留痕）、G-005（条件不可解析→condition_error 状态 + critical） |
| `core/entity.py` | 改 | G-006：缺表/缺列跳过留痕（info）、插件加载失败留痕（warning）后再回退 |
| `core/audit.py` | 改 | G-007（version=unknown 标 anchor_status=missing）、G-018（新增 chain_integrity() 结构化自检，chain_verify bool 保留） |
| `core/case_library.py` | 改 | G-007：版本缺失留痕 |
| `core/derived.py` | 改 | G-001（异常失效令牌改一次性值强制重算）、G-007（版本缺失留痕） |
| `core/hypotheses.py` | 改 | G-008（dimension_coverage 双轨 declared/empirical）、G-009（`<=` 阈值 + 枚举缺口文案）、G-011（维度读声明）、G-012（ENUM_SPACE 读声明） |
| `core/metrics.py` | 改 | G-017：override_rate 超阈值产 warning 诊断（阈值入 thresholds.json） |
| `core/anomaly_channel.py` | 新 | G-019：诊断→异常线索（同构、级别恒待核实、needs_human_review 恒真、不参与交叉） |
| `core/geo.py` | 新 | G-021：地点标准化/别名归一 + 可选 geocode 距离判定；无坐标降级标注 |
| `scripts/init_duckdb.py` | 改 | G-014：冷层建表从 bindings 源表名 + objects 属性 TYPE_SQL 推导（不 import core） |
| `scripts/export_ladybug.py` | 改 | G-015：边导出按 links endpoints 声明通用生成 |
| `run_all.py` | 改 | G-010：创建 RunHealth 贯穿管线，report 首部插"健康度"小节 |
| `ontology/<pack>/dimensions.json` | 新 | G-011：维度声明（name/说明/source_object_types） |
| `ontology/<pack>/enum_space.json` | 新 | G-012：枚举空间维度与取值声明 |
| `ontology/default/objects.json` / `links.json` | 改 | G-013（可选 jian 字段）、G-015（links endpoints 端点列名） |
| `ontology/default/policies.json` | 改 | G-016：schema_version 1→2 |
| `schemas/policies.schema.json` | 改 | G-016：const 版本同步为 2 |
| `ontology/default/case_knowledge.json` | 改 | G-016：补 schema_version: 2 |
| `ontology/default/thresholds.json` | 改 | G-017：override_rate 告警阈值声明 |
| `schemas/`（dimensions/enum_space 等） | 新 | G-011/012：新声明文件的 JSON Schema |
| `tests/test_run_health.py` | 新 | G-010 + 留痕汇总：健康度小节字段/计数/键序 |
| `tests/test_rule_zero_diag.py` | 新 | G-002：四类 zero_type 互不混淆、不进 findings |
| `tests/test_empty_degrade.py` | 新 | G-003：空转降级三 AC |
| `tests/test_event_trace.py` | 新 | G-004：publish 失败留痕 + 死信汇总 |
| `tests/test_wake_condition.py` | 新 | G-005：条件不可解析→condition_error |
| `tests/test_entity_trace.py` | 新 | G-006：缺列/坏插件留痕且不阻断 |
| `tests/test_version_anchor.py` | 新 | G-007：unknown 锚定标记 + 计数 |
| `tests/test_derived.py` | 改 | G-001：异常令牌非常量、跨对象隔离回归 |
| `tests/test_coverage_empirical.py` | 新 | G-008/009：双轨独立、0.8 边界报警、缺口文案 |
| `tests/test_audit_integrity.py` | 新 | G-018：chain_verify 回归 + 记录数/字段完备性 |
| `tests/test_dispatch_failclosed.py` | 新 | G-020：派发失败→dispatch_failed |
| `tests/test_override_alert.py` | 新 | G-017：推翻率超阈告警含规则标识 |
| `tests/test_decl_config.py` | 新 | G-011/012/013/014/016：声明化 + 版本收口 + 硬失败负向 |
| `tests/test_anomaly_channel.py` | 新 | G-019：异常线索同构/恒待核实/不参与交叉 |
| `tests/test_geo.py` | 新 | G-021：地点归一/距离判定/无关不误判/无坐标降级 |
| `run_tests.py` | 改 | 分批注册新测试组 |

## Implementation Steps（依赖顺序）

**第一波（无依赖，先铺底座）：G-010 → G-002/003 → G-004/005/006/007 → G-001**

1. **G-010 统一健康度层（先做，是其余项出口）**
   - Step 1.1：`core/run_health.py` —— `run_diagnostic` 建表走运行时表模式（与 ensure_runtime_tables 同类，编译器跳过）；字段 `run_id/seq/kind/severity/source/reason/detail(JSON)/created_at`。`RunHealth(conn, run_id=None).record(kind, severity, source, reason, **detail)`；`NullRunHealth.record()` 空操作；`summary()` 按 kind/severity 聚合并查表（event_dead_letter COUNT 等）；`health_section()` 返回固定字段小节。
   - Step 1.2：**兼容红线**——所有接入点签名 `health: RunHealth | None = None`，None 时落 NullRunHealth；现有不传参调用行为零变化。
   - Step 1.3：`run_all.py` 管线入口建 `RunHealth`，沿 run_rules/FunctionExecutor/action/deferred/entity 显式传参；report 组装（L229-238）时 `"健康度"` 键置于 findings/结论字段之前。
   - Step 1.4：`tests/test_run_health.py` —— AC1 小节字段完整；AC2 注入 N 条降级计数一致；AC3 序列化后"健康度"键序在结论前；AC4 health=None 时全链路不报错。

2. **G-002 规则零命中诊断**
   - Step 2.1：`functions.json` function 增可选 `diagnostics: {input_tables: [obj_*...], config_missing_flag: "<输出字段名>"}`；loader 校验 input_tables 是已声明对象（未知名硬失败）。
   - Step 2.2：`core/rules.py:run_rules` —— 去掉 L118-119 的裸 continue；每条规则无论命中与否：按 input_tables `COUNT(*)` 得 scan_rows、`len(out.rows)` 得 matched_rows；zero_type 四分类——scan_rows=0→`data_absent`；函数输出 config_missing 标志为真（如敏感地点集合空）→`config_missing`；scan>0 且 matched=0 且非 config_missing→`empty_result_suspect`；规则在 rules.json 显式 `zero_is_clean:true` 才标 `clean_scan`（机器不擅自下排除结论）。诊断 `health.record("rule_zero_hit", ...)`，**不 append 进 findings**。
   - Step 2.3：`tests/test_rule_zero_diag.py` —— AC1-4 四类 zero_type；AC5 四类互不混淆；AC6 findings 长度不受诊断影响。

3. **G-003 空转降级**
   - Step 3.1：`core/functions.py:FunctionExecutor.invoke` —— 在 CatalogException 分支（L468-478）旁加统一判据：正常返回后若 `scan_rows>0 且（sql rows 为空 或 py impl 结果空）`→ 返回 `degraded=true, degraded_reason="输入非零、输出为零，疑似匹配失效"` 并 `health.record("function_empty_degraded","warning",...)`。
   - Step 3.2：CatalogException 路径保持不变（AC2）；scan_rows=0 不标匹配失效（AC3，归 G-002 data_absent）。
   - Step 3.3：`tests/test_empty_degrade.py` —— 500 行→0 行 degraded 且 reason 含标识；表不存在走原降级；0→0 不标。

4. **G-004 事件发布留痕**
   - Step 4.1：`core/event_bus.py` —— publish 落盘段（L130-136）包 try，失败时不再静默：上抛或提供 `publish_or_record()`；新增 `dead_letter_count()` 查询。
   - Step 4.2：`core/action_executor.py:_publish`（L291-292）与 `core/deferred.py`（L154-155、L188-189）的 `except Exception: pass` 改为 `except Exception as e: health.record("event_publish_failed","warning",source=...,reason=str(e))`，不阻断主流程。
   - Step 4.3：health summary 增 event_publish_failed 计数 + 一次性 event_dead_letter_summary。
   - Step 4.4：`tests/test_event_trace.py` —— DROP event_log 制造落盘失败，断言诊断可检索、报告含"事件发布失败 N 条"；正常路径审计记录不变。

5. **G-005 唤醒条件显式化**
   - Step 5.1：`core/deferred.py:match_wake`（L110-116）—— `except (TypeError, ValueError)` 不再 pass：`health.record("wake_condition_unparseable","critical",source=f"deferred:{task_id}",...)` 并把任务置 `status='condition_error'`（区别于 waiting 沉睡），人审队列可见。
   - Step 5.2：`tests/test_wake_condition.py` —— evidence_count 传不可转换值→condition_error + 进诊断；正常匹配不变。

6. **G-006 实体解析留痕**
   - Step 6.1：`core/entity.py` —— 缺表/缺列 continue（L382-386 等）前插 `health.record("entity_table_skipped","info",source=f"table:{t}",reason=缺列名/表不存在)`；插件 exec_module 失败（L576-579）与 ImportError 回退（L585）→ `health.record("entity_plugin_failed","warning",source=f"plugin:{path}",reason=str(e))` 后再 continue 回退内置规则。
   - Step 6.2：`tests/test_entity_trace.py` —— 缺列表断言记录含表名+列名；坏插件断言 warning 且仍以内置规则继续（不阻断）。

7. **G-007 版本锚定显式化**
   - Step 7.1：`core/audit.py`（L112）、`core/case_library.py`（L143）、`core/derived.py`（L96）—— 取版本失败仍可回退 "unknown" 不崩，但同时 `health.record("version_anchor_missing","warning",...)`；audit_chain 写入 version=unknown 时在 after_state 内标 `"anchor_status":"missing"`（不改表结构，旧库兼容）。
   - Step 7.2：`tests/test_version_anchor.py` —— 版本表不可用→审计记录标 anchor_status=missing；报告计数与记录数一致；正常版本写入不变。

8. **G-001 缓存失效令牌（随第一波）**
   - Step 8.1：`core/derived.py:_source_version_set`（L90-102）—— 版本/行数异常分支不返回常量：改 `f"unknown::{obj_type}::{uuid4().hex}"` 一次性令牌，保证与任何历史令牌不等→强制 miss 重算；并 `health.record("version_anchor_missing",...)`。
   - Step 8.2：`tests/test_derived.py` —— 双失败时**同一对象**连续两次令牌不等、cache=miss、命中率 0；回归断言不同 obj_pks 的 params_hash 本就不同（跨对象隔离）；正常 until_source_change 命中行为与 golden 不变。

**第二波（依赖第一波留痕数据）：G-016（可先做）→ G-008/009 → G-017/018 → G-020**

9. **G-016 策略版本收口（独立，可先做）**
   - Step 9.1：`policies.json` schema_version 1→2，同步 `schemas/policies.schema.json` 的 const；`case_knowledge.json` 补 `"schema_version": 2`；二者纳入 loader/validate 统一版本校验，版本不符硬失败。
   - Step 9.2：`tests/test_decl_config.py`（版本收口部分）—— 全部声明文件版本一致；版本不符硬失败；现有用例全绿。

10. **G-008 覆盖度双轨 + G-009 阈值边界**
    - Step 10.1：`core/hypotheses.py:dimension_coverage`（L247-257）—— 增参 `findings=None`，返回 `{"declared":{score,covered,missing},"empirical":{score,covered,missing}}`；empirical.covered = 该维度下确有 finding 产出的维度集合；两轨独立计算并列。
    - Step 10.2：判据 `score < DIMENSION_ALARM`（L255）改 `score <=`；alarm_text（L256-257）改用 L250 已算出的 missing 枚举维度名 + 各维度规则命中数。
    - Step 10.3：`tests/test_coverage_empirical.py` —— 声明含而实证不含（AC1）；两轨独立（AC2）；命中则两轨均含（AC3）；score=0.8 报警、1.0 不报、文案含缺口维度名。

11. **G-017 推翻率告警**
    - Step 11.1：`thresholds.json` 增 override_rate 阈值声明（如 0.5）；`core/metrics.py` 在 override_rate（L150）计算后，超阈→`health.record("override_rate_alert","warning",source=f"rule:{rid}",rate=...)`，按规则粒度。
    - Step 11.2：`tests/test_override_alert.py` —— 超阈告警含规则标识；未超不告警。

12. **G-018 审计链完整性自检（扩展既有）**
    - Step 12.1：`core/audit.py` —— 保留 `chain_verify()->bool`（L154-185 签名/断链校验不动）；新增 `chain_integrity()->{chain_ok, expected_count, actual_count, broken_links, missing_fields[]}`，missing_fields 含 operator/after_state 空值及 ontology_version=unknown（联动 G-007）；结论进健康度。
    - Step 12.2：`tests/test_audit_integrity.py` —— AC1（回归）篡改可检出；AC2 必填字段缺失/unknown 被检出计数；AC3 记录数不符给差异；AC4 结论进健康度。

13. **G-020 派发 fail-closed**
    - Step 13.1：`core/action_executor.py`（L230-231）—— `except ImportError` 不再置 dispatching：改 `status='dispatch_failed'` + `health.record("dispatch_failed","critical",...)`；正常 outbox 入队流转不变。
    - Step 13.2：`tests/test_dispatch_failclosed.py` —— outbox 不可用→dispatch_failed 而非 dispatching + critical；正常路径状态流转不变。

**第三波（依赖第二波健康度）：G-011~015 声明化 → G-019 → G-021**

14. **G-011 维度声明化**
    - Step 14.1：新增 `ontology/<pack>/dimensions.json`（`{name, 说明, source_object_types[]}`）+ schema；loader 装载校验，rules 引用未声明 dimension → 硬失败；`hypotheses.DIMENSIONS`（L67）改为读声明。
    - Step 14.2：`tests/test_decl_config.py` —— dimensions.json 存在且过校验；未声明维度被硬失败；新增维度不改 Python 即被规则引用并参与覆盖度。

15. **G-012 枚举空间配置化**
    - Step 15.1：新增 `ontology/<pack>/enum_space.json`，`hypotheses.ENUM_SPACE`（L119-125）移出代码读声明；`enumerate_space(space=...)` 传参覆盖保留。
    - Step 15.2：测试——定义来自声明文件；新增取值不改代码即入候补池；自定义 space 传参仍覆盖。

16. **G-013 五间映射声明化（红线：等级规则硬编码）**
    - Step 16.1：objects/links 增可选 `jian` 字段（枚举生间/内间/反间/死间/因间…），loader 枚举校验非法值硬失败；`core/functions.py` 五间交叉遍历 pack 声明替代 `_JIAN_MAP`（L228）。
    - Step 16.2：**红线**——交叉等级规则（单源=观察/双源=线索/三源+=可立案依据候选）与间类展示顺序**保持硬编码不进配置**；测试加负向断言（改配置试图改等级无效/非法 jian 硬失败）。

17. **G-014 冷层建表推导（红线：不 import core）**
    - Step 17.1：`scripts/init_duckdb.py`（L30）硬编码清单改为从 bindings 源表名 + objects 属性 TYPE_SQL 映射推导建表；预聚合表保持手工。
    - Step 17.2：**红线 AC4**——init_duckdb 不得 import core（避免循环依赖），类型映射用独立最小常量或读 JSON；测试断言新增数据源仅在 bindings 声明即生成空表、列类型与物化列一致、预聚合表不受影响。

18. **G-015 端点列名形式化**
    - Step 18.1：links.json 各 link 增 `"endpoints": {from_col, to_col, ...}`；loader 校验；`scripts/export_ladybug.py` 改按声明通用生成边 SQL。
    - Step 18.2：golden 逐条比对迁移前后导出一致；新增链接不改导出脚本即可导出。

19. **G-019 异常线索通道（红线：不参与交叉）**
    - Step 19.1：`core/anomaly_channel.py` —— 从 run_diagnostic 可转化类（rule_zero_hit 的 empty_result_suspect、function_empty_degraded、coverage_gap）生成异常线索：与 finding 同构（带 source_rows，空则带 diagnostic_ids 溯源），强制 `"级别":"待核实"`、`needs_human_review=true`、`is_anomaly=true`；进人审队列、进审计；按主体聚合。
    - Step 19.2：**红线**——五间交叉函数显式 `WHERE is_anomaly IS NOT TRUE` 过滤，异常绝不贡献交叉命中、绝不升格。
    - Step 19.3：`tests/test_anomaly_channel.py` —— 同构且携溯源；级别恒待核实/needs_human_review 恒真；构造异常条目后交叉等级不变（AC3）；按主体聚合。

20. **G-021 地点标准化**
    - Step 20.1：`core/geo.py` —— ① 地址归一/别名（去"东侧50米/K12+300"修饰、提取路名主干）；② 可选 geocode 转坐标 + 距离阈值；以只读 **Function** 注册（functions.json + FUNCTION_IMPLS），如 `location_colocated(loc_a, loc_b, radius_m=...)`；无坐标/无法解析→降级标注不报错（经 G-002/003 留痕）。
    - Step 20.2：同框 link build_sql（bindings.json L108）后续改调标准化 function 或温层预计算标准化列。
    - Step 20.3：`tests/test_geo.py` —— 「滨江路中段 K3+200」与「滨江路」判同距/同地点；无关地点不误判；可被规则 function 挂钩；无坐标降级不报错。

**收尾**

21. **登记测试组 + 全量验证**
    - run_tests.py 分批注册：第一波 runhealth/rulezerodiag/emptydegrade/eventtrace/wakecond/entitytrace/versionanchor；第二波 coverageempirical/auditintegrity/dispatchfailclosed/overridealert/declconfig；第三波 anomalychannel/geo。
    - 每波完成后 WSL 跑 `python run_tests.py` 全绿 + `python -m scripts.mcp_client_test` 不退化。
    - `python run_all.py --auto-review --no-cli` 冒烟：产物 output JSON 首部出现"健康度"小节，六类静默点有计数。

## Dependencies and Considerations

- **统一降级协议，不是 21 个补丁**：所有留痕走 `core/run_health.py` 单一出口；接入点一律 `health=None` 可选注入 + NullRunHealth 空操作，**现有 48 组测试不传参即零行为变化**——这是不引发回归的关键。
- **失败可见 ≠ 失败容错（红线 3）**：本计划把 `except: pass`/裸 `continue` 改为"留痕后继续"，**不**把任何硬失败改为放行。未知名硬失败、loader 交叉校验、fail-closed 一律保持。
- **交叉等级规则硬编码（红线 1）**：G-013 只把"对象→间类"的映射声明化，等级规则（单/双/三源）与展示顺序绝不进配置。
- **异常线索不参与交叉（红线 2）**：G-019 异常条目 is_anomaly=true，交叉计算显式过滤；级别恒待核实、needs_human_review 恒真，只有人能区分"数据缺失"与"刻意规避"。
- **既有能力不重复建设**：event_dead_letter 表（event_bus）、chain_verify（audit）、CatalogException 降级（functions）直接复用/扩展，G-004/G-018 只补缺口那半段。
- **clean_scan 不自动判定**：零命中默认 `empty_result_suspect`（疑似失效），只有规则显式 `zero_is_clean:true` 才标 clean_scan——机器不擅自产出"排除性结论"，契合侦查语境"未验证 ≠ 无异常"。
- **运行时表不丢**：run_diagnostic 照 ensure_runtime_tables 模式建表，编译器重建语义层不清诊断；诊断进 run_diagnostic 表与产物，**不进 findings 主列表**。
- **最小化侵入**：新模块 run_health/anomaly_channel/geo + 新声明 JSON；既有模块仅在静默点插留痕调用，不改既有函数返回契约（degraded 字段为 append）。
- **范围控制**：严格按 REQ-G.md 各 AC 条实现，不顺手扩展（geo 不做完整 GIS、anomaly_channel 不做自动升格、dimensions 不做维度权重推理）。

## Validation

- 每 REQ 单测对应 AC 条数（新增）：G-010 4 / G-002 6 / G-003 3 / G-004 2 / G-005 2 / G-006 2 / G-007 3 / G-001 3（含 1 回归）/ G-008 3 / G-009 3 / G-016 3 / G-017 2 / G-018 4 / G-020 2 / G-011 3 / G-012 3 / G-013 3（含等级硬编码负向）/ G-014 4 / G-015 3 / G-019 4 / G-021 4 ≈ **60+ 项新增单测**。
- `python run_tests.py` 每波全绿（48 组基线，逐波递增）。
- `python -m scripts.mcp_client_test` 保持不退化（MCP 工具契约不变）。
- `python run_all.py --auto-review --no-cli` 冒烟：output 报告 JSON 首部含"健康度"小节；人为制造缺列/坏插件/空转/版本表缺失时，对应计数 > 0 且 findings 主列表不被诊断污染；异常线索不改变交叉等级。
- 红线专项负向测试全绿：改 links/objects 非法 jian 硬失败、policies 版本错硬失败、规则引用未声明维度硬失败、异常条目进交叉无效、init_duckdb import core 被断言拦截。

## Risks

- **健康度小节改变产物结构**：run_all 产物 JSON 顶部新增"健康度"键，golden baseline 若对 report 做整体快照比对会 diff。应对：golden 比对 findings/交叉等级等业务字段，健康度小节为新增独立键（旧消费者忽略未知键）；如需刷新 baseline 属有意升级。
- **零命中诊断量膨胀**：每条规则每维度都产诊断，run_diagnostic 行数可能大。应对：诊断按 (run_id, rule_id, zero_type) 聚合存明细计数，不逐行存；产物只带汇总 + 非 clean 项明细。
- **G-003 空转误报**：规则合法返回 0 行（本就无异常）会被标 degraded。应对：`zero_is_clean:true` 显式声明 + empty_result_suspect 只是 warning 级疑似信号，不阻断、不改 finding，仅提示人工复核。
- **G-014 推导建表类型映射漂移**：从 objects TYPE_SQL 推导冷层列类型，若映射不全可能建错列。应对：init_duckdb 不依赖 core，类型映射独立维护并与 TYPE_SQL 加一致性测试；推不出的表回退显式声明并告警。
- **G-021 地点归一误合并**：模糊匹配可能把不同地点判同。应对：距离阈值保守 + 无法解析降级标注而非强行匹配；geo function 结果同样进待核实，不自动升格。
- **声明化迁移期双轨**：dimensions/enum_space/jian 声明与旧硬编码短期并存可能不一致。应对：loader 装载期交叉校验，硬编码读取点改为"声明优先、缺省回退并告警"，一个包完全迁移后删除回退。
