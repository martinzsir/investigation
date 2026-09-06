# REQ-D 数据治理 D0–D6 全量落地总结报告

> 生成日期：2026-09-06　分支：`ontology_v2`　最终提交：`490bf70`
> 范围：REQ-D-001 ~ REQ-D-022 共 **22 项需求**，分 7 个批次（D0–D6）全部落地
> 累计变更：**45 个文件，+6572 / -71 行**（c69aee0 → 490bf70，三次功能提交）
> 最终回归：**92 组测试全绿（含 e2e）**

---

## 一、总览

| 批次 | 提交 | 需求数 | 需求编号 | 新增测试组 | 回归规模 |
|------|------|--------|----------|------------|----------|
| D0–D4 | `1561c02` | 10 | 001/004/005/008/009/010/012/013/016/018 | 10 组 | 78 组全绿 |
| D5（贯通汇聚） | `c55d773` | 8 | 002/003/006/007/011/017/021/022 | 7 组 | 85 组全绿 |
| D6（P2 增强） | `490bf70` | 4 | 014/015/019/020 | 4 组 | **92 组全绿（含 e2e）** |

设计主线：**声明是数据、实现是代码**——治理能力全部挂在 ontology 案件包 JSON 声明上（data_elements / bindings 扩展字段 / clean_rules），检测器与构建器代码零业务化；所有治理结果"失败被表达成数据"，统一落 RunHealth（`run_diagnostic`），只告警不阻断侦查管线。

---

## 二、分批落地明细

### D0–D4 批（`1561c02`，25 文件 +4136/-48）

| 需求 | 内容 | 核心落点 | 测试组 |
|------|------|----------|--------|
| REQ-D-012 | 1:1 绑定约束守护（loader 装载期 fail-closed） | `core/ontology_loader.py` | one2one |
| REQ-D-001 | 数据元标准注册（`data_elements.json`，15 数据元） | `core/data_elements.py` | dataelements |
| REQ-D-004 | 清洗 op 注册表（原子 op 双实现：声明式+py） | `core/clean_ops.py` | opregistry |
| REQ-D-005 | 属性级清洗作用域声明（缺省继承 name_property） | bindings 扩展 | cleanscope |
| REQ-D-009 | transform 层（脏值可用性抢救，清洗后仍保原值） | bindings `transform` | transform |
| REQ-D-010 | on_cast_error 三态：null（缺省）/ fail（硬中断）/ quarantine（整行隔离落 `build_quarantine`，含脱敏样本+name_value） | `core/ontology.py` 编译期 | casterror |
| REQ-D-013 | 复合列显式降级（声明拆分规则，未声明诊断留痕） | `core/ontology_loader.py` | composite |
| REQ-D-016 | 值域与格式合规扫描（引用数据元 check 定义） | `core/compliance.py` | compliance |
| REQ-D-008 | 清洗留痕（剔除计数+脱敏样本，剔除率>30% 升 warning） | `core/run_health.py` | cleanstats |
| REQ-D-018 | 敏感字段启发式扫描（证件号/手机号遮蔽预检） | `core/sensitive_scan.py` | sensitivescan |

接线：`run_all.py` 新增 6.6 构建后质量门（合规+敏感），`run_tests.py` 注册 10 组。

### D5 批（`c55d773`，23 文件 +1605/-103）

| 需求 | 内容 | 核心落点 | 测试组 |
|------|------|----------|--------|
| REQ-D-002 | 属性引用数据元（`prop_de_clean` 装载期展开继承，binding 显式声明优先） | `core/ontology_loader.py` / `ontology.py` | de_ref |
| REQ-D-003 | 代码表接入（数据元 enum 派生标准代码表；`enum_space` 移除硬编码人名「主体」维度，庙算组合 240→60；非法枚举装载期硬失败；版本快照 append-only） | `ontology/default/data_elements.json`、`enum_space.json` | codetable |
| REQ-D-006/011 | 声明式带参 op（param_enum 白名单，reject_if 等；6 个双实现归一 op：digits_only/strip_cc/strip_paren/to_upper/to_lower/pad_date） | `core/clean_ops.py` | declarative_ops |
| REQ-D-007 | 清洗词表外置（`clean_rules.json` merge/replace，CleanContext 替代硬编码词表） | `core/clean_ops.py` | wordlist |
| REQ-D-017 | 合规违规率进画像五要素扣分（SCORE_COMPLIANCE 三档回退） | `core/ontology_profile.py` | compliance_score |
| REQ-D-021 | 数据元驱动落点推荐（列名别名+值模式→数据元+clean/transform 建议；只进 draft 提案、低置信标需人工、混装列给上游拆分提示） | `core/de_recommend.py` | recommend |
| REQ-D-022 | 四类结果统一进 RunHealth（clean/quarantine/compliance/sensitive 汇聚，by_source 来源计数，异常降级 degraded） | `core/run_health.py` | health_integration |

配套：`scripts/scan_hardcoded_names.py` 新增枚举去人名红线扫描（AC-2）。

### D6 批（`490bf70`，11 文件 +915/-4）

| 需求 | 内容 | 核心落点 | 测试组 |
|------|------|----------|--------|
| REQ-D-014 | 属性级空值策略 allow/reject/quarantine（**空值先于 CAST**：真空值走 null_policy，CAST 失败走 on_cast_error，二者不重叠） | `core/ontology.py` `_apply_null_policy` | null_policy（5 用例） |
| REQ-D-015 | 业务键去重（`dedup_key` + on_conflict：keep_latest/keep_first/fail；**代理键分配之前**按业务键收敛，冲突规模落健康度 kind=dedup_key_conflict） | `core/ontology.py` `_apply_dedup_key` | dedup_key（6 用例） |
| REQ-D-019 | 数据时间新鲜度（含时间属性对象 max(date) 与当前日期比较，超期 stale_days 落诊断 kind=data_freshness_stale；与本体版本新鲜度分离） | `core/data_freshness.py` | data_freshness（5 用例） |
| REQ-D-020 | 单位/口径一致性（金额数据元跨表量级比对，元/万元混用提示 kind=unit_mismatch；缺单位声明单独列出；只提示不阻断） | `core/unit_scan.py` | unit_consistency（4 用例） |

接线：`run_all.py` 6.6 质量门扩展为四扫（合规+敏感+新鲜度+单位），`run_tests.py` 注册 4 组。

---

## 三、体系化成果

### 3.1 运行时管线（构建期顺序）

```
源表 → 缺列预检(必填硬失败/可选降级) → null_policy(REQ-D-014)
    → 编译期 TRY_CAST/硬 CAST(on_cast_error 三态，REQ-D-010)
    → clean(清洗 op 注册表+外置词表，REQ-D-004/006/007) → transform(REQ-D-009)
    → dedup_key 去重(REQ-D-015) → 代理键分配 → obj_* 物化
    → 构建后质量门：合规(016)+敏感(018)+新鲜度(019)+单位(020)
    → 全部结果进 RunHealth(022) → 画像扣分(017) → 推荐落 draft(021)
```

### 3.2 新增/改造核心文件

| 文件 | 职责 | 引入批次 |
|------|------|----------|
| `core/data_elements.py` | 数据元装载（`ontology/<pack>/data_elements.json`） | D1 |
| `core/clean_ops.py` | 清洗 op 注册表（原子+带参双实现归一，外置词表） | D1/D5 |
| `core/compliance.py` | 值域/格式合规扫描 | D4 |
| `core/sensitive_scan.py` | 敏感列启发式扫描 | D4 |
| `core/de_recommend.py` | 数据元驱动落点推荐（只进 draft） | D5 |
| `core/data_freshness.py` | 数据时间新鲜度扫描 | D6 |
| `core/unit_scan.py` | 单位/口径一致性扫描 | D6 |
| `core/ontology.py` | 编译管线扩展（cast_error/null_policy/dedup_key/clean/transform） | D3/D5/D6 |
| `core/ontology_loader.py` | 声明装载校验（1:1 约束、复合列、数据元引用展开、新扩展字段 fail-closed 解析） | D0–D6 |
| `core/ontology_profile.py` | 画像五要素 + 合规违规率扣分 | D4/D5 |
| `core/run_health.py` | 统一健康度出口（新增 clean_drop_rate / source_value_quarantined / dedup_key_conflict / data_freshness_stale / unit_mismatch 等诊断类型） | D4–D6 |
| `run_all.py` | 6.5 构建统计落账 + 6.6 构建后质量门（四扫） | D4/D6 |
| `run_tests.py` | GROUPS 注册 21 个 REQ-D 测试组 | 全程 |
| `ontology/default/data_elements.json` | 15 个标准数据元（含值域 check 与标准代码表派生） | D1/D5 |

### 3.3 测试覆盖

- REQ-D 专属测试组 **21 组 / 约 90 用例**，全部注册 `run_tests.py` GROUPS
- 全量回归 92 组（含 e2e、golden、mcp 等）零回归通过
- 红线保障：非法枚举/未知清洗 op/未知空值状态/未知去重策略/未声明复合列 → 装载期硬失败（fail-closed）

---

## 四、提交与惯例

| 提交 | 说明 |
|------|------|
| `1561c02` | feat(ontology): REQ-D 数据治理 D0-D4 批次落地 |
| `c55d773` | feat(ontology): REQ-D 数据治理 D5 批次（贯通汇聚）落地 |
| `490bf70` | feat(ontology): REQ-D 数据治理 D6 批次（P2 增强）落地 |

均推送至 `origin/ontology_v2`。数据产物（`data/ladybug/*.lbug`、`data/samples/*.xlsx`）与 web 设计文档（`.trae/documents/web/*`）按惯例排除在提交之外。

## 五、遗留与后续

- REQ-D 22 项需求无功能缺口；D6 后无已知 FAIL 项。
- 参数上线接线（提案审批自动生效）属后续批次计划，非 REQ-D 范围。
- 单位一致性/新鲜度阈值为缺省值（ratio_threshold=10000、stale_days=180），可按案件包后续参数化。
