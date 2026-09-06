# REQ-D 实施方案（修正基线版）

**依据**：[REQ-D.md](./REQ-D.md) 经代码核对评估后的修正基线。评估结论：文档可信度声明 10 条中 9 条成立，7 处需修订后方可实施；本方案已将修订吸收进各批次。

**验证命令**（WSL 环境，每批完成必须全绿）：

```bash
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
```

新测试组同步注册 `run_tests.py` 的 GROUPS 注册表。改 MCP 相关加跑 `python -m scripts.mcp_client_test`。

---

## 〇、前置工作：REQ-D.md 基线修订（纯文档，先行）

| # | 位置 | 修订内容 |
|---|---|---|
| 1 | 009/010 业务目标、可信度声明表 | L350 CAST 声明标注"B2-08 后已改 TRY_CAST + 脏值计数（core/run_health.py record_build_dirty）"；REQ-D-009 重述为"脏值可用性抢救"（`48,000.00` → 可用的 48000.00，而非降级 NULL），优先级 P0 → P1 |
| 2 | 009 管线图 | 实际顺序是 SQL 内 TRY_CAST 先执行、Python 侧 clean 后执行（core/ontology.py `_compute_object_rows`：先 fetch 已 CAST 行，再 `_apply_clean`）。改为"SQL 投影内 transform → TRY_CAST；Python 侧 clean 仅字符串列" |
| 3 | 010 AC-4/AC-6 | 默认态改 `null`（保持 B2-08 已验收行为），`quarantine` 为属性级显式声明；`fail` 定义为可回退的历史语义而非"现状" |
| 4 | 012 AC-1/AC-5 | 限定"**同一 binding 内**一源列一属性"；明确 source_sql 属 AC-3 派生属性豁免（person/account 现有 UNION 绑定不违规）；跨对象共用源列合法（通话记录.主体 同时喂 person/call） |
| 5 | 001 示例与硬依赖 | 示例 `schema_version` 改 2（与其余 13 个声明文件一致）；硬依赖补 REQ-D-004（clean_rule 交叉引用需注册表先行） |
| 6 | 013 AC-2 | 执行点改为"物化后值扫描检出 + 画像告警"（loader 编译期无法静态判定复合列） |
| 7 | 006/011 | 原子 op 合并为同一注册表，clean/transform 两层只是注入点不同 |

---

## 一、总体架构决策（5 条，全案贯穿）

### AD-1 投影改写管道（技术核心）

typed_raw 投影渲染从 `TRY_CAST("raw" AS T)` 升级为 `TRY_CAST(<transform_expr> AS T)`，transform 表达式由 op 注册表编译（声明式 op → `regexp_replace`/`trim`/`lpad` 等 SQL 函数）。未声明 transform 的属性渲染不变（009 AC-5 向后兼容）。

- 改动点：`core/ontology_loader.py` 的 `_compile_structured_source`；`core/ontology.py` 的 `ObjectBinding`（新增 `transform` 字段，形态同 clean 的属性级映射）。
- 红线：链接 build_sql 只表达关系不变；transform 只作用于结构化源的属性投影。

### AD-2 唯一 op 注册表

新增 `core/clean_ops.py`：

```python
OPS: dict[str, OpSpec]   # {name: {impl: "sql"|"py", sql_template?, fn?, params_schema?}}
```

- `CLEAN_RULE_NAMES` 改为从注册表派生的动态集合（现有 `core/ontology.py:200` 两处引用签名不变）。
- `strip` / `exclude_org_tokens` 作为 py op 注册，行为回归锁定。
- REQ-D-006（clean 层声明式规则）与 REQ-D-011（transform 层内置原子 op）全部注册于此，禁止两套并行实现。

### AD-3 清洗双通道（滤行 + 改值）

现状 `_apply_clean` 只有滤行能力（strip 仅作过滤谓词，不回写值）。升级为：

- py op 返回 `(value, keep)` 二元组——字符串列**改值生效**，剔除计数进 `stats["clean_stats"]`。
- `clean_stats` 结构：`{object, property, rule, dropped_rows, sample_masked}`，落健康度（REQ-D-008 全部 AC）。

### AD-4 隔离表是构建副作用，不是语义对象

`build_quarantine(run_id, object, property, sample_masked, reason, source)`：

- 由编译器/扫描器写入，编译器照常 DROP/CREATE 管理（与 obj_* 同口径）。
- 进 RunHealth 健康度小节（REQ-D-022），**不进 obj_*/lnk_* 语义层**。
- 全案零业务写操作：Function/Action/ActionExecutor 体系完全不动。

### AD-5 数据元继承走 loader 展开，权限仍 fail-closed

- 属性 `{"data_element": "DE_X"}` 在 loader 装载期展开，继承 type/format/checksum/clean_rule。
- **数据元 `sensitive: true` 时该属性必须在 policies.json 已声明遮蔽，否则硬失败**（比原文 AC-4"自动进候选"更严格，与 REQ-009/010/011 未声明即拒绝对齐）。
- 未引用数据元的属性行为完全不变（002 AC-5）。

---

## 二、分批实施（批 D0–D6）

### 批 D0：1:1 约束守护（REQ-D-012，P0，无依赖，最先做）

| 项 | 内容 |
|---|---|
| 改动 | `core/ontology_loader.py` binding 装载校验：同一 binding 的 `source.columns` 内同一源列映射两个别名 → 硬失败（错误信息报冲突双方）；transform 声明含 `split` 类 op → 硬失败并提示"上游 source_sql 处理"（AC-4 出路提示） |
| 明确豁免 | source_sql 绑定 = 派生属性（person/account 现状不违规）；跨对象共用源列合法 |
| 测试组 | `test_one2one`（8 例：同列双映射拒、split 拒、13 声明文件全过、出路提示可读、跨对象共用放行、UNION 豁免） |
| 回归 | 全量 73 组 + ontology/e2e 着重 |

### 批 D1：数据元标准 + op 注册表（REQ-D-001 ∥ REQ-D-004，P0）

| 项 | 内容 |
|---|---|
| 001 | 第 14 声明文件 `data_elements.json`（schema_version=2）；loader 新增装载校验：必填字段缺失硬失败（AC-1）、未知 checksum 硬失败（AC-2）、元素 ID 重复硬失败（AC-5）、`clean_rule` 必须已在 op 注册表（与 binding→clean 同口径交叉校验）；checksum 算法先注册 `idcard_mod11` 一个 |
| 004 | `CLEAN_RULE_NAMES` 派生自注册表；重复注册硬失败（AC-2）；`strip`/`exclude_org_tokens` 行为回归锁定（AC-4）；`core/draft_assembler.py` 的 `clean_available` 自动扩充（AC-5） |
| 测试组 | `test_data_elements`（7 例）、`test_op_registry`（6 例） |

### 批 D2：清洗作用域 + transform 层（REQ-D-005 + REQ-D-009，共用 AD-1 投影管道）

> 005 与 009 **必须同批**：两者共用同一套属性级投影/改写机制，拆开做会重复搭管道。

| 项 | 内容 |
|---|---|
| 005 | binding `clean` 扩展属性级映射（数组形式 = `{"_name": [...]}` 向后兼容，AC-3）；`_apply_clean` 改双通道（AD-3）：解除 `otype.name != "person"` 与 `r[0]` 两条硬限制（AC-1/AC-2）；规则按声明顺序执行（AC-4）；未声明属性不动（AC-5） |
| 009 | binding 新增 `transform`（属性级映射，仅声明式 op）；typed_raw 投影按 AD-1 注入；未知 op 硬失败（AC-6） |
| 测试组 | `test_clean_scope`（10 例）、`test_transform`（8 例，用 48,000.00 / ￥1,280.50 / 2024年3月15日 样例） |

### 批 D3：CAST 三态 + 复合列（REQ-D-010、REQ-D-013，依赖 D2）

| 项 | 内容 |
|---|---|
| 010 | 属性级 `on_cast_error` 三态：`fail`（渲染 CAST 的回退能力）/ `null`（默认，现状）/ `quarantine`（TRY_CAST 为 NULL 的行 → INSERT 进 `build_quarantine` 含失败原因 + 脱敏样本，AD-4）；三态计数全部进健康度（AC-3/AC-5 不静默） |
| 013 | 声明 `composite: true` 的属性：类型层打标"不可参与实体关联"——loader 校验其不得作为 link normalize 的 ON 键（AC-3，编译期可判定）；composite 属性不参与 pk/去重（AC-5）；复合列**检出**走画像期值扫描（含分隔符模式），检出 → 告警提示上游拆分或显式 composite；画像显示"未拆分"状态（AC-4） |
| 测试组 | `test_cast_error`（9 例：2 正常 + 2 隔离 + 2 置 NULL 混合装载）、`test_composite`（7 例） |

### 批 D4：质量检查三件套（REQ-D-016 + 008 + 018）

| 项 | 内容 |
|---|---|
| 016 | 新增 `core/compliance.py`（只读，走 gateway 消费 obj_*，不直读 Parquet）：按数据元 format/checksum/range/代码表对已物化属性扫描。**不静默机制**：违规行以"对象.属性 + 代理键 + 违规码"落 run_diagnostic（可下钻），聚合违规率进画像——不动 obj_* schema；检查项可单独启停（声明 `compliance_checks`，AC-6）；未引用数据元的属性不扫（AC-8） |
| 008 | AD-3 的 `clean_stats` 落健康度（AC-1/2/5）；样本脱敏（AC-3）；剔除率 >30% 升 warning（AC-4）；画像页每属性清洗前后行数（AC-6） |
| 018 | 列名启发式（id_card/phone/card/证件/手机）+ 值模式（18 位身份证/11 位手机/16-19 位卡号形态）扫描未声明遮蔽的敏感列；对照 property_policies 去重（AC-2）；输出"建议补充遮蔽声明"（AC-4）；**只告警不阻断**（AC-5）；误报率可测（AC-3，用 D6 案例的陷阱列量化） |
| 测试组 | `test_compliance`（10 例）、`test_clean_stats`（6 例）、`test_sensitive_scan`（7 例） |

### 批 D5：贯通汇聚（REQ-D-002 + 003 + 006 + 007 + 011 + 017 + 021 + 022）

| 项 | 内容 |
|---|---|
| 002 | 属性引用数据元（AD-5 展开继承；本地声明与数据元冲突硬失败 AC-2；未知 ID 硬失败 AC-6） |
| 003 | `enum_space.space` 支持从数据元代码表派生；`scan_hardcoded_names` 复验无人名（AC-2）；案件级可追加不覆盖（AC-4）；pack 快照锁定不受代码表变更影响（AC-5） |
| 006/011 | 声明式 op（regex/strip_cc/pad_date/to_upper/to_lower/reject_if/default）注册进唯一注册表（AD-2），006 在 clean 层、011 在 transform 层启用；17 类脏格式样例固化为测试数据 |
| 017 | 合规违规率进画像扣分：`SCORE_COMPLIANCE` 独立档，code 前缀 `compliance_`（五要素复用 `core/ontology_profile.py` `_deduct`）；无合规属性时分数不变（AC-5 回归） |
| 021 | `scripts/profile_table.py` 落点推荐升级为数据元驱动（列名别名 + 值模式 → 推荐数据元 + clean/transform 规则）；**只进 draft 提案，永不自动生效**（AC-3 红线，与"提案审批不自动生效"项目约束一致） |
| 022 | clean_stats / quarantine / compliance / sensitive_scan 四类结果统一进 RunHealth 小节，独立 `source` 标识（`record()` 已支持，AC-5）；健康度状态因数据质量降级（AC-6）；MCP 侧若加查询工具需过 mcp_client_test |
| 测试组 | `test_de_ref`（6 例）、`test_codetable`（5 例）、`test_declarative_ops`（8 例）、`test_wordlist`（5 例）、`test_compliance_score`（5 例）、`test_recommend`（6 例）、`test_health_integration`（7 例） |

### 批 D6：P2 增强（REQ-D-014、015、019、020）

各自独立小改动：

- **014 空值策略**：属性级 `null_policy` 三态（allow/reject/quarantine），与 on_cast_error 组合优先级：空值先于 CAST。
- **015 业务键去重**：binding `key: {columns, on_conflict: keep_latest|keep_first|fail}`；注意与事件代理键内容哈希的交互——去重先于代理键分配。
- **019 数据新鲜度**：按对象时间属性 MAX() 对比当前日期（与本体版本 FRESH/STALE 分开显示）；空时间属性不误报。
- **020 单位一致性**：数据元声明 unit + 跨表一致性扫描；只提示不定性。

测试组各 4-6 例：`test_null_policy`、`test_dedup_key`、`test_data_freshness`、`test_unit_consistency`。

---

## 三、风险与护栏

1. **最大风险**：AD-1 动了 typed_raw 编译路径——`test_dirty_date` / `test_missing_column` / `spec` 组是回归雷区（历史上 spec 组曾因临时 pack 状态污染闪失败，需独立复跑定位）。
2. `on_cast_error` 默认保持 `null`，不改 B2-08 已验收语义；切 `quarantine` 是显式声明行为。
3. 全案零 LLM、零直读 Parquet（扫描器走 gateway 消费 obj_*）、零业务写路径。
4. 数据产物（parquet/lbug/xlsx/csv）不入 git；提交按主题分批。
5. 提交流程遵循项目约定：分拣功能文件、消息文件避开 PowerShell heredoc、Windows 原生 git 推送、引用级别核对。

## 四、建议节奏

```
第〇节基线修订 → D0/D1 并行 → D2（技术核心，预留最充分验证）→ D3 → D4 → D5
→ 业务测试案例全量跑（见《REQ-D业务测试案例_海州电诈案.md》）→ D6
```
