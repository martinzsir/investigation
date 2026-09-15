# 五间解耦实施计划

> 目标：将"五间"从内核硬编码概念降级为 default 案件包的一个配置实例，使内核成为通用的"多源交叉验证引擎"。

## 1. 现状：耦合全景

### 1.1 已声明化（改 JSON 不改 Python）

| 耦合点 | 文件 | 状态 |
|--------|------|------|
| 间类名/权重/clearance | `ontology/default/jians.json` | 纯声明 |
| 展示顺序 | `core/functions.py:254` `_jian_order()` | 从 jians.json 读 |
| 交叉等级名称 | `core/functions.py:264` `_cross_level_name()` | 名称可配，1/2/3 映射硬编码 |
| 线索打分权重 | `core/lineage.py:206` `_jian_weights()` | 从 jians.json 读 |
| 源独立性对 | `jians.json source_independence` | 纯声明 |
| 维度声明 | `ontology/default/dimensions.json` | 纯声明（五间外的侦查维度，已解耦） |
| 计分维度权重 | `ontology/default/scoring.json` | 纯声明 |

### 1.2 硬编码耦合（需改 Python）

| # | 耦合点 | 文件:行 | 性质 | 影响面 |
|---|--------|---------|------|--------|
| H1 | `DEFAULT_JIANS` 常量 | `core/ontology_loader.py:79` | 兜底常量 | 装载/校验 |
| H2 | 对象 `jian` 字段必填校验 | `core/ontology_loader.py:1020-1027` | 类型层绑定 | 对象装载 |
| H3 | 链接 `jian` 字段校验 | `core/ontology_loader.py:129` (传 `allowed_jian`) | 类型层绑定 | 链接装载 |
| H4 | 规则 `jian_types` 校验 | `core/ontology_loader.py:2103` | 规则层绑定 | 规则装载 |
| H5 | 1/2/3 源→等级映射 | `core/functions.py:367` | 核心算法 | 交叉等级 |
| H6 | `JIAN_ALL` 类属性默认值 | `core/hypotheses.py:69` | 检测引擎 | 假设覆盖检查 |
| H7 | `cross_level()` 硬编码等级 | `core/lineage.py:162-176` | 线索等级 | 线索报告 |
| H8 | `_cross_level_name_single()` 硬编码 | `core/lineage.py:141-159` | 线索等级 | 线索优先级 |
| H9 | `_jian_weights()` 回落默认值 | `core/lineage.py:212` | 打分兜底 | 线索排序 |
| H10 | `JIAN_ORDER` 常量 | `core/ontology_profile.py:214` | L4 质量画像 | 本体画像 |
| H11 | `ROLE_CLEARANCE` 间类密级 | `core/access.py:44-51` | 权限面 | 访问控制 |
| H12 | `can_see_jian_types()` | `core/access.py:59-71` | 权限面 | 线索可见性 |
| H13 | impact 分析 `jians` 条目 | `core/impact.py:42` | 影响面 | 变更影响分析 |
| H14 | `ObjectType.jian` / `LinkType.jian` 字段 | `core/ontology.py:80,143` | 类型声明 | 数据模型 |
| H15 | `RuleSpec.jian_types` 字段 | `core/ontology.py:227` | 规则声明 | 数据模型 |
| H16 | `SkillSpec.consumes_jian` | `core/registry.py:136` | 技能注册 | 线索生成 |
| H17 | `LineageClue.jian_types` | `core/registry.py:166` | 线索模型 | 全链路 |
| H18 | `anomaly_channel` 排除五间 | `core/anomaly_channel.py:16,103,188` | 异常通道 | 线索过滤 |
| H19 | LLM redact `jian_types` 出网 | `core/llm/redact.py:258` | LLM 隔离 | 脱敏输出 |
| H20 | LLM guard `jian_types` 白名单 | `core/llm/guard.py:165` | LLM 隔离 | 字段白名单 |
| H21 | LLM draft_rule 提示词 | `core/llm/draft_rule.py:5,92,95` | LLM 草拟 | 规则生成 |
| H22 | `_jian_entries()` 收集器 | `core/functions.py:308-329` | 数据源收集 | 交叉等级函数 |
| H23 | `jian_cross_level` Function | `core/functions.py:332-381` | 只读 Function | 检测器编排 |
| H24 | scoring.json `jian_coverage` 维度 | `ontology/default/scoring.json:12` | 计分维度 | 线索排序 |
| H25 | `store.l1` 注释 | `core/store.py:67` | 注释 | 无行为影响 |

## 2. 改造分三期

### 原则

- **向后兼容**：default 案件包行为不变，测试全绿
- **声明优先**：Python 代码只读 JSON 声明，不硬编码业务值
- **最小切口**：每期独立可验证，不跨期混合改动

### 分期总览

```
第一期（解绑类型层）  → 第二期（泛化语义）  → 第三期（可插拔算法）
  对象 jian 字段        jians → source_groups      cross_levels 阈值声明化
  可选化 + 跳过          全量重命名                hypotheses 覆盖检查可插拔
  影响 8 个文件         影响 16 个文件             影响 4 个文件
```

---

## 3. 第一期：解绑对象类型层（jian 字段可选化）

**目标**：没有 `jians.json` 的案件包也能跑，对象/链接不声明 `jian` 不报错，检测器对无 `jian` 的对象跳过五间逻辑。

### 3.1 `core/ontology.py`

| 行号 | 改动 | 说明 |
|------|------|------|
| 80 | `jian: str = ""` → 保持不变，加注释 | 字段保留，但从必填降为可选 |
| 143 | `jian: str = ""` → 同上 | 链接侧同理 |
| 227 | `jian_types: tuple[str, ...] = ()` → 保持不变 | 规则侧已有默认空元组 |

**不改字段定义**——`jian` 字段在数据模型层保留，只是装载校验放松。

### 3.2 `core/ontology_loader.py`

| 行号 | 当前代码 | 改为 | 说明 |
|------|---------|------|------|
| 79 | `DEFAULT_JIANS = ["因间", "内间", "反间", "死间", "生间"]` | `DEFAULT_JIANS: list[str] = []` | 空列表=无五间，不再硬编码间类名 |
| 1027 | `allowed_jian = allowed_jian or set(DEFAULT_JIANS)` | `allowed_jian = allowed_jian if allowed_jian is not None else set()` | 不再回落到硬编码五间 |
| 1027 后 | — | 新增：`# jian 字段可选：allowed_jian 为空集时不校验` | — |
| ~1095 | `_parse_jian(...)` 调用处 | 包裹：`if allowed_jian:` 才校验 | jian 为空串且 allowed_jian 为空 → 跳过 |
| 129 | `links = _load_links(..., allowed_jian=allowed_jian)` | 同步改 `_load_links` 内部 | 同上逻辑 |
| 2103 | `_jians = allowed_jian if allowed_jian is not None else set(DEFAULT_JIANS)` | `_jians = allowed_jian or set()` | 规则侧同步 |
| 2120-2122 | `bad_jian = set(jian) - _jians` + `if bad_jian: raise` | `if _jians:` 前置条件 | 无声明间类集时跳过校验 |

### 3.3 `core/ontology_loader.py` — `load_jians()`

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 221-243 | jians.json 缺失 → 回落 `DEFAULT_JIANS` | jians.json 缺失 → 返回 `[]` | 无五间声明=不用五间 |
| 226-243 | 校验逻辑 | 保持不变 | 有 jians.json 时照常校验 |

### 3.4 `core/functions.py`

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 255-261 | `_jian_order()` 回落 `DEFAULT_JIANS` | 回落 `[]` | 无声明=空顺序 |
| 308-329 | `_jian_entries()` | `entries` 为空时直接返回 `[]` | 已有此行为，确认即可 |
| 332-381 | `jian_cross_level` | `entries` 为空 → 返回 `{"rows": [], "命中间类": [], "独立源数": 0, "交叉等级": "未配置", ...}` | 无五间=不计算交叉等级 |

### 3.5 `core/hypotheses.py`

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 69 | `JIAN_ALL = ["生间", "反间", "因间", "死间", "内间"]` | `JIAN_ALL: list[str] = []` | 无声明=空集 |
| 152 | `self.JIAN_ALL = [j["name"] for j in jians]` | 保持不变 | 有声明时覆盖 |
| 325 | `expected = expected or self.JIAN_ALL` | `expected = expected if expected is not None else self.JIAN_ALL` | 区分 None 和空列表 |
| 327-330 | `missing = [j for j in expected if j not in covered]` | `if not expected: return {"covered": [], "missing": [], "warnings": []}` | 无间类声明=跳过覆盖检查 |

### 3.6 `core/lineage.py`

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 162-176 | `cross_level()` 硬编码 `n >= 3 / n == 2 / n == 1` | `if not clues: return "未配置"` 前置 + 保持映射 | 无线索=无等级 |
| 206-212 | `_jian_weights()` 回落硬编码 | 回落 `{}` | 无声明=空权重表 |
| 265 | `jian = sum(jian_weight.get(j, 1) for j in c.jian_types)` | `jian = sum(jian_weight.get(j, 0) for j in c.jian_types)` | 无权重=0 贡献（当前默认 1 应改 0） |
| 266 | `jian_cov = min(1.0, jian / jian_normalize)` | `jian_cov = min(1.0, jian / jian_normalize) if jian_normalize else 0.0` | 防除零 |
| 301 | `n_jians = len(set(c.jian_types))` | 保持不变 | 空集=0 → `_cross_level_name_single` 返回 None |
| 141-159 | `_cross_level_name_single()` | `n <= 0: return None` 已有 | 确认即可 |

### 3.7 `core/anomaly_channel.py`

| 行号 | 当前 | 说明 |
|------|------|------|
| 16 | `"绝不参与五间交叉等级计算"` | 注释更新为 `"绝不参与交叉等级计算"` |
| 103 | `"jian_types": []` | 保持不变（空列表=不归属任何间类） |
| 188 | `"剔除异常线索"` | 保持不变 |

### 3.8 `core/access.py`

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 44-51 | `ROLE_CLEARANCE` 硬编码角色密级 | 保持不变 | 角色秩级是访问控制概念，非五间概念 |
| 54-56 | `jian_clearance_for_role()` | 保持不变 | 通用函数，不硬编码间类名 |
| 59-71 | `can_see_jian_types()` | `if not jian_clearances: return True` 前置 | 无间类声明=全部可见 |

### 3.9 `core/ontology_profile.py`

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 214 | `JIAN_ORDER = ("因间", "内间", "反间", "死间", "生间")` | 从 `load_jians(pack)` 动态读取；缺失=空元组 | 不再硬编码 |
| 508-527 | `_jian_map()` | `JIAN_ORDER` 为空 → 返回 `{"forward": {}, "reverse": []}` | 无五间=空画像 |

### 3.10 `core/impact.py`

| 行号 | 当前 | 说明 |
|------|------|------|
| 42 | `"jians": ("jians", "name", "jian_removed")` | 保持不变（影响面分析条目，jians.json 存在时才触发） |
| 709 | `"jians"` 在 ANALYZABLE_FILES 中 | 保持不变（jians.json 不存在则无影响面） |

### 3.11 验证标准

- `python run_tests.py` 全绿（138 组）
- 无 jians.json 的精简测试包不报错
- `jian_cross_level` 在无五间声明时返回空结果不崩溃
- default 案件包行为不变（五间照常计算）

---

## 4. 第二期：泛化语义（jians → source_groups）

**目标**：将 Python 代码中所有 `jian` / `JIAN` / `间` 引用统一重命名为 `source_group` / `group` / `source`，`jians.json` → `source_groups.json`。算法不变，纯重命名。

### 4.1 文件重命名

| 原文件 | 新文件 | 说明 |
|--------|--------|------|
| `ontology/default/jians.json` | `ontology/default/source_groups.json` | 声明文件重命名 |
| `ontology/*/jians.json` | `ontology/*/source_groups.json` | 所有案件包同步 |

### 4.2 JSON 结构重命名

```json
// source_groups.json（原 jians.json）
{
  "schema_version": 2,
  "source_groups": [
    {"name": "因间", "default_clearance": 1, "weight": 3, "source_object_types": ["org", "bid_project"]}
  ],
  "cross_levels": [...],
  "source_independence": {...}
}
```

字段 `jians` → `source_groups`。其余字段名不变（`name`/`weight`/`default_clearance`/`source_object_types`/`cross_levels`/`source_independence`）。

### 4.3 Python 全量重命名映射

| 原名 | 新名 | 涉及文件 |
|------|------|---------|
| `DEFAULT_JIANS` | `DEFAULT_SOURCE_GROUPS` | ontology_loader.py |
| `load_jians()` | `load_source_groups()` | ontology_loader.py |
| `load_cross_levels()` | 不变 | ontology_loader.py |
| `load_source_independence()` | 不变 | ontology_loader.py |
| `allowed_jian` | `allowed_source_groups` | ontology_loader.py |
| `_jian_order()` | `_source_group_order()` | functions.py |
| `_jian_entries()` | `_source_group_entries()` | functions.py |
| `jian_cross_level` (function id) | `source_cross_level` | functions.py + functions.json + rules.json |
| `JIAN_ALL` | `SOURCE_GROUPS_ALL` | hypotheses.py |
| `jian_coverage()` | `source_group_coverage()` | hypotheses.py |
| `_jian_weights()` | `_source_group_weights()` | lineage.py |
| `_cross_level_name_single()` | 不变 | lineage.py |
| `JIAN_ORDER` | `SOURCE_GROUP_ORDER` | ontology_profile.py |
| `_jian_map()` | `_source_group_map()` | ontology_profile.py |
| `jian_clearance_for_role()` | `source_group_clearance_for_role()` | access.py |
| `can_see_jian_types()` | `can_see_source_groups()` | access.py |
| `ROLE_CLEARANCE` | 不变 | access.py |
| `ObjectType.jian` | `ObjectType.source_group` | ontology.py |
| `ObjectType.jian_source` | `ObjectType.source_group_label` | ontology.py |
| `LinkType.jian` | `LinkType.source_group` | ontology.py |
| `LinkType.jian_source` | `LinkType.source_group_label` | ontology.py |
| `RuleSpec.jian_types` | `RuleSpec.source_groups` | ontology.py |
| `SkillSpec.consumes_jian` | `SkillSpec.consumes_source_groups` | registry.py |
| `LineageClue.jian_types` | `LineageClue.source_groups` | registry.py |
| `by_jian()` | `by_source_group()` | registry.py |

### 4.4 scoring.json 维度重命名

| 原 | 新 | 说明 |
|----|-----|------|
| `jian_coverage` | `source_group_coverage` | 计分维度名 |
| `jian_weight_sum` | `source_group_weight_sum` | 数据源标识 |
| `jian_normalize` | `source_group_normalize` | 变量名 |

### 4.5 objects.json / links.json 字段重命名

```json
// objects.json
{
  "name": "transaction",
  "source_group": "反间",          // 原 "jian": "反间"
  "source_group_label": "银行流水"  // 原 "jian_source": "银行流水"
}
```

### 4.6 rules.json 字段重命名

```json
{
  "id": "R6",
  "source_groups": ["反间"]    // 原 "jian_types": ["反间"]
}
```

### 4.7 LLM 相关文件重命名

| 文件 | 改动 |
|------|------|
| `core/llm/redact.py:258` | `jian_types` → `source_groups` |
| `core/llm/redact.py:266` | 注释 `间类` → `源分组` |
| `core/llm/guard.py:165` | `jian_types` → `source_groups` |
| `core/llm/draft_rule.py:5,92,95` | 提示词 `间类` → `源分组`，`jian_types` → `source_groups` |

### 4.8 前端重命名

| 文件 | 改动 |
|------|------|
| `GenericConfigView.vue` | 无直接引用（间接通过 scoring.json） |
| `frontend/src/**/*.ts` | grep `jian` 全量替换 |
| `frontend/src/**/*.vue` | grep `jian` 全量替换 |

### 4.9 验证标准

- `python run_tests.py` 全绿
- `python -m scripts.mcp_client_test` 全绿
- `jians.json` 文件不存在，`source_groups.json` 正常加载
- grep `jian\|JIAN\|间` 在 `core/` 下零命中（注释中的"五间"概念词保留）

---

## 5. 第三期：交叉验证逻辑可插拔

**目标**：把 `1/2/3 源→等级` 映射从硬编码改为 `cross_levels` 声明驱动，`hypotheses` 覆盖检查改为按 source_groups 声明驱动。

### 5.1 `core/functions.py` — 交叉等级映射声明化

当前硬编码（第 367 行）：
```python
level_n = 3 if n >= 3 else (2 if n == 2 else 1)
level = _cross_level_name(level_n, pack)
```

改为：
```python
levels = load_cross_levels(pack)  # 已按 min_independent_sources 排序
level = "未配置"
for lv in sorted(levels, key=lambda x: x["min_independent_sources"], reverse=True):
    if n >= lv["min_independent_sources"]:
        level = lv["name"]
        break
```

`cross_levels.json` 已声明 `min_independent_sources` 为 1/2/3，此改为从声明读取映射关系而非硬编码。`min_independent_sources` 的值可配（如某案件包要 5 源才升"可立案依据候选"）。

### 5.2 `core/lineage.py` — `cross_level()` 同步声明化

当前硬编码（第 172-176 行）：
```python
if n >= 3:
    return "可立案依据候选"
if n == 2:
    return "线索"
return "观察"
```

改为调用 `_cross_level_name_single(n, pack)`（已有函数，只是当前内部仍硬编码）。

`_cross_level_name_single()` 内部同步改为声明驱动（同 5.1 逻辑）。

### 5.3 `core/hypotheses.py` — 覆盖检查可插拔

当前 `jian_coverage()` 检查"每个间类至少被 1 条假设引用"。

改为：
```python
def source_group_coverage(self, expected: list[str] | None = None) -> dict:
    groups = expected if expected is not None else self.SOURCE_GROUPS_ALL
    if not groups:
        return {"covered": [], "missing": [], "warnings": []}
    # ... 原逻辑
```

无 `source_groups` 声明时跳过覆盖检查，不报"间类未覆盖"警告。

### 5.4 `ontology/default/source_groups.json` — 扩展声明

```json
{
  "schema_version": 2,
  "source_groups": [...],
  "cross_levels": [
    {"min_independent_sources": 1, "name": "观察"},
    {"min_independent_sources": 2, "name": "线索"},
    {"min_independent_sources": 3, "name": "可立案依据候选"}
  ],
  "source_independence": {...}
}
```

`cross_levels` 的 `min_independent_sources` 从"硬编码映射"变为"声明阈值"，案件包可自定义（如 `min_independent_sources: 5` 才升"可立案依据候选"）。

### 5.5 验证标准

- `python run_tests.py` 全绿
- 修改 `cross_levels` 的 `min_independent_sources` 值后，交叉等级按新阈值生效
- 无 `source_groups` 声明的案件包，覆盖检查不报警

---

## 6. 实施顺序与依赖

```
第一期（类型层解绑）
  ├── 3.2 ontology_loader.py      ← 先改（核心入口）
  ├── 3.3 load_jians()             ← 同步
  ├── 3.4 functions.py             ← 依赖 3.3
  ├── 3.5 hypotheses.py            ← 依赖 3.3
  ├── 3.6 lineage.py               ← 依赖 3.3
  ├── 3.7 anomaly_channel.py       ← 无依赖，可并行
  ├── 3.8 access.py                ← 无依赖，可并行
  ├── 3.9 ontology_profile.py      ← 依赖 3.3
  ├── 3.10 impact.py               ← 无依赖，可并行
  └── 验证：run_tests.py 全绿
        ↓
第二期（语义泛化）
  ├── 4.1-4.2 文件重命名 + JSON 结构    ← 先改
  ├── 4.3 Python 全量重命名             ← 依赖 4.1
  ├── 4.4 scoring.json 字段重命名       ← 依赖 4.1
  ├── 4.5-4.6 objects/links/rules 字段  ← 依赖 4.1
  ├── 4.7 LLM 文件重命名               ← 依赖 4.3
  ├── 4.8 前端重命名                   ← 依赖 4.4
  └── 验证：run_tests.py + mcp_client_test 全绿
        ↓
第三期（算法可插拔）
  ├── 5.1 functions.py 交叉等级声明化   ← 先改
  ├── 5.2 lineage.py 同步              ← 依赖 5.1
  ├── 5.3 hypotheses.py 覆盖检查      ← 无依赖，可并行
  ├── 5.4 source_groups.json 扩展     ← 依赖 5.1
  └── 验证：run_tests.py 全绿
```

## 7. 风险与红线

### 7.1 不可改

| 红线 | 说明 |
|------|------|
| `min_independent_sources` 映射语义 | 1→观察/2→线索/3→可立案依据候选 是业务语义不变量；第三期只是让"阈值可配"，映射关系本身不变 |
| `ROLE_CLEARANCE` 角色秩级 | 访问控制的行政秩级（0-4），与五间 `default_clearance` 是两把尺子，不合并 |
| `cross_levels` 升级方向 | 只能升不能降（三源一定包含双源的信息量），不支持自定义降级 |

### 7.2 需注意

| 风险 | 缓解 |
|------|------|
| 第二期重命名量大（~50 处） | 用 `replace_all` 批量改，改完跑全量测试 |
| MCP 工具引用 `jian_cross_level` function id | 第二期同步改 `functions.json` + `rules.json` + MCP 客户端测试 |
| 前端可能有 `jian` 引用 | 第二期 grep 前端全量替换 |
| `LineageClue.jian_types` 序列化到产物 JSON | 第二期改后旧产物 `jian_types` 字段消失，需前端兼容或一次性迁移 |

### 7.3 不在范围内

| 项 | 说明 |
|----|------|
| `DIMENSIONS` 维度声明 | 已声明化（dimensions.json），与本改造无关 |
| `assumption_confidence` | 已声明化（scoring.json），与本改造无关 |
| `enum_space` | 已声明化，与本改造无关 |
| 案件包 `pack_meta` | 案件包元数据，与本改造无关 |

## 8. 改造前后对比

### 改造前

```
对象必须声明 jian 字段
  → loader 校验 jian ∈ DEFAULT_JIANS
  → functions._jian_order() 硬编码五间
  → hypotheses.JIAN_ALL 硬编码五间
  → lineage._jian_weights() 硬编码回落
  → access.can_see_jian_types() 硬编码密级
  → ontology_profile.JIAN_ORDER 硬编码顺序
  → LLM redact/guard/draft_rule 硬编码字段名
```

### 改造后

```
对象可选声明 source_group 字段
  → loader 校验 source_group ∈ source_groups.json（无声明=跳过）
  → functions._source_group_order() 从 source_groups.json 读
  → hypotheses.SOURCE_GROUPS_ALL 从 source_groups.json 读
  → lineage._source_group_weights() 从 source_groups.json 读
  → access.can_see_source_groups() 从 source_groups.json 派生
  → ontology_profile.SOURCE_GROUP_ORDER 从 source_groups.json 读
  → LLM redact/guard/draft_rule 统一用 source_groups 字段名

"五间"只是 default 案件包 source_groups.json 的一个配置实例。
换案件包可以完全不用五间概念，内核是通用的多源交叉验证引擎。
```
