# 孙武侦查官 · DerivedProperty（派生属性）能力说明与使用边界

> 版本 **v1.0** ｜ 日期 2026-09-10
> 依据：`core/derived.py`（REQ-028）源码核查 + 装载/缓存/守门逻辑
> 定位：**"查询时才算"的属性**——不物化进 obj_ 表，用时调白名单函数计算
> 核心提醒：**用错会直接冲击红线三（事实/推断分栏）**，边界见 §4

---

## 0. 结论先行

| 问题 | 答案 |
|---|---|
| 已存在吗？ | ✅ **已完整实现**（`core/derived.py`，REQ-028），但**未暴露**（无声明文件、无 API） |
| 适用指标吗？ | ✅ **适用，但必须是"确定性聚合"，不是打分** |
| 适用标签吗？ | ✅ **适用，但必须是事实型/阈值型，不能是定性型** |
| 用错的风险？ | 🔴 **冲击红线三**——推断值混入事实栏 |
| 最大约束？ | **AC5 命名禁令**：启发式打分不许进语义层 |

---

## 1. 它是什么

### 1.1 定义

```python
@dataclass
class DerivedProperty:
    name: str            # "person.transaction_count"（obj_type.property）
    function: str        # 白名单 Function 名（functions.json 已注册）
    inputs: list[str]    # function 参数 → 派生输入的映射
    cache_policy: str    # never | ttl | until_source_change | materialized
    ttl_seconds: int = 60
```

**核心特征**：**查询时派生**（REQ-028 标题即"查询时派生"），不物化进 `obj_*` 表。

### 1.2 返回结构

```python
{
  "value": ...,                  # 计算值（rows 或 result）
  "computed_at": float,          # 计算时刻
  "source_version_set": str,     # 源版本锚点（版本号::行数）
  "params_hash": str,            # 参数哈希（缓存键）
  "cache": "hit" | "miss"        # 是否命中缓存
}
```

**注意 `source_version_set`** —— 带版本锚点，结果可追溯到"基于哪个版本算的"。

---

## 2. 三道守门（比其他模块严格）

| 守门 | 机制 | 作用 |
|---|---|---|
| **AC3 白名单** | `prop.function` 必须在 `functions.json` 已声明 | 只能调只读白名单函数 |
| **AC5 命名禁令** | `_FORBIDDEN_REGISTRY_NAMES` | 启发式打分**禁止**注册 |
| **cache_policy 校验** | 四种策略，非法即报错 | 防止策略错配 |

### 2.1 AC5 禁令原文

```python
_FORBIDDEN_REGISTRY_NAMES = {
    "person.risk_score", "org.risk_score", "person.risk", "clue.score",
}
```

模块 docstring 的解释：

> "person.risk_score 这种**启发式打分**不进入 registry（AC5 断言），
> 保持在展示层/MCP 层临时计算，**避免进入对象属性的稳定语义层**。"

**这与红线三（三栏不合并）是同一思路**——防止推断污染事实。

---

## 3. 四种缓存策略

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `never` | 每次重算 | 实时性要求极高 |
| `ttl` | `ttl_seconds`（默认 60s）内复用 | 有一定容忍度 |
| **`until_source_change`** | **源版本+行数没变就不重算** | ⭐ **指标/标签首选** |
| `materialized` | 与 `obj_{type}` 派生列同步 | 高频访问 |

### 3.1 设计亮点：锚点失败不复用陈旧值

`_source_version_set()` 的注释记录了两个历史坑：

1. 曾误用不存在的 `core.ontology.get_ontology_version` → 版本锚点**从未真正取到**（恒 unknown）
2. 修复原则：**版本或计数任一不可靠 → 返回一次性随机令牌强制 miss 重算**，绝不复用陈旧缓存
3. 同时落 `version_anchor_missing` 诊断（REQ-G-007）

> **这是个很成熟的失效处理设计**，与 `FeatureStore` 的 hash+stale 思路同源。

---

## 4. 🔴 使用边界（最重要）

### 4.1 指标：适合，但分两种

| 类型 | 例子 | 可注册？ |
|---|---|---|
| **确定性计数/聚合** | 交易笔数、通话次数、账户余额、首次交易日期 | ✅ **适合** |
| **启发式打分** | 风险分、可疑度、综合评级 | ❌ **AC5 禁止** |

**判断标准**：能否用一句确定的计算规则说清楚？

```
交易笔数 = COUNT(*)                        → 能 → 可注册 ✅
风险分 = 0.3×A + 0.7×B（权重人定）         → 权重主观 → 不可注册 ❌
```

**为什么卡这么死**：风险分是**可调的启发式**——今天权重 0.3、明天 0.5。
若它成为"对象属性"，**对象语义会随参数漂移**，稳定语义层就被污染了。

### 4.2 标签：再细分为三种

| 类型 | 例子 | 判断 |
|---|---|---|
| **事实型** | "是否公职人员"（权威名单比对） | ✅ 可注册 |
| **计算型（阈值型）** | "高频交易者"（次数 > 阈值，阈值在声明里） | ✅ 可注册 |
| **定性型** | "高风险客户"、"疑似关联方" | ❌ **禁止** |

**第三种危害最大**：若 `person.is_suspect` 成为对象属性，它**混进了事实栏**——
这正是红线三要防的。

> **一个好用的判据**：这个标签**能不能作为呈堂证据？**
> 不能的，就不该进语义层。

### 4.3 判断树

```
想加一个派生属性
  │
  ├─ 能用确定的 SQL/函数算出来吗？
  │    └─ 否（含主观权重、模型打分）→ ❌ 禁止，放展示层
  │
  ├─ 它是定性结论吗？（涉嫌/可疑/高风险）
  │    └─ 是 → ❌ 禁止（红线一：AI 不产定性结论）
  │
  ├─ 参数（阈值/权重）在声明文件里吗？
  │    └─ 否 → ⚠️ 先外置到 thresholds.json，否则不可审计
  │
  └─ 全部通过 → ✅ 可注册
```

### 4.4 推荐缓存策略

| 场景 | 策略 | 理由 |
|---|---|---|
| 指标（交易笔数） | **`until_source_change`** | 随数据变，不随时间变 |
| 标签（是否高频） | **`until_source_change`** | 同上 |
| 实时性要求高 | `never` | — |

---

## 5. 典型用途

### 5.1 避免物化爆炸 ⭐

"每个人的交易笔数"——物化要在 `obj_person` 加列并在每次 BUILD 重算；
派生则**查询时算**。

**对"30 张表 3000 列"场景特别有意义**：大量"偶尔才看"的属性不该占物化成本。

### 5.2 跨对象聚合

`person.transaction_count` 需聚合 `obj_transaction`——
这类跨对象计算**不适合放进对象自身定义**。

### 5.3 保持语义层纯净

AC5 的用意：**稳定对象语义 vs 临时启发式计算**分开。
风险分/打分放展示层；真正属于对象本质的才进 registry。

### 5.4 带版本锚点的可追溯结果

返回含 `source_version_set`——**能追溯到"基于哪个版本算的"**，与审计链天然契合。

---

## 6. 启用路径（当前未暴露）

### 6.1 现状

| 检查项 | 结果 |
|---|---|
| 实现 | ✅ 完整（`core/derived.py`） |
| 测试 | ✅ 有（`tests/test_derived.py`、`test_version_anchor.py`） |
| 声明文件 | ❌ 无 |
| 生产调用 | ❌ 仅测试引用 |
| API / MCP / UI | ❌ 全无 |

**注册方式目前只能在 Python 代码里调 `register()`** —— 无声明式入口。

### 6.2 建议两步启用

**第一步：加声明文件**

```jsonc
// ontology/default/derived_properties.json
{
  "schema_version": 1,
  "_note": "派生属性声明。查询时派生，不物化；function 必须在 functions.json 已注册；"
           "name 不得为启发式打分（AC5 禁令见 core/derived.py）。",
  "properties": [
    {
      "name": "person.transaction_count",
      "function": "integer_transfer_aggregates",
      "inputs": ["obj_transaction"],
      "cache_policy": "until_source_change"
    },
    {
      "name": "person.is_frequent_caller",
      "function": "call_frequency_spike",
      "inputs": ["obj_call"],
      "cache_policy": "until_source_change"
    }
  ]
}
```

**第二步：集成进 RuntimeContext**

```python
class RuntimeContext:
    def derived(self, obj_type: str, prop: str, **params):
        """派生属性访问（受 AC5 禁令与白名单守门）。"""
        return compute(self.store, obj_type, prop, params=params,
                       pack=self.pack, health=self.health)
```

### 6.3 装载期校验（建议新增）

| 校验 | 说明 |
|---|---|
| `name` 格式 | 必须为 `obj_type.property` |
| `function` 已声明 | 复用 AC3 白名单 |
| **AC5 禁令** | 命中 `_FORBIDDEN_REGISTRY_NAMES` 硬失败 |
| `cache_policy` 合法 | 四选一 |
| `inputs` 引用存在 | 表/对象已声明 |

---

## 7. ⚠️ 风险提醒

### 7.1 别当成"给对象加字段"的通用机制

AC5 说明设计者意图是**严格限制**：只有真正属于对象语义的才能注册。

若开放成"随便加计算字段"，**会直接冲击红线三**——
因为派生值本质是**算出来的**，属于推断。

### 7.2 与 RuntimeContext 的关系

派生属性**需要运行时才能算**（要 store、pack、health）。
**两者是配套的**——建议在 RuntimeContext 一期就预留 `derived()` 接口。

### 7.3 与 3000 列性能问题的关系

派生属性是解法的一部分：

| 属性类型 | 处理 |
|---|---|
| 高频访问 | 物化 |
| 偶尔查看的**确定性**指标/标签 | 派生，查询时算 |
| 启发式打分 | ❌ 只能放展示层，不能指望派生机制 |

**若 3000 列里大量是启发式打分，派生帮不上忙**——那些必须留在展示层。

---

## 8. 验收标准

| 编号 | 用例 | 预期 |
|---|---|---|
| DP-TC-01 | 注册确定性指标（交易笔数） | ✅ 成功 |
| DP-TC-02 | 注册 `person.risk_score` | ❌ **硬失败**（AC5） |
| DP-TC-03 | 注册 `person.is_suspect` | ❌ 拒绝（定性标签） |
| DP-TC-04 | function 未在 functions.json 声明 | ❌ 硬失败（AC3） |
| DP-TC-05 | `cache_policy` 非法 | ❌ 硬失败 |
| DP-TC-06 | `until_source_change`：源未变 | 返回 `cache: "hit"` |
| DP-TC-07 | 源变化后 | 返回 `cache: "miss"`，重算 |
| DP-TC-08 | **版本锚点失败** | **强制重算**，落 `version_anchor_missing` 诊断 |
| DP-TC-09 | 返回值 | 含 `source_version_set`（可追溯） |
| DP-TC-10 | 换包（对象改名） | 装载期校验发现引用失效 |

---

## 9. 一句话总结

> **派生属性适用于指标和标签——但必须是"算得清"的确定性聚合，不能是打分；
> 必须是事实型或阈值型标签，不能是定性结论。**
>
> 边界就写在 AC5 那四个被禁的名字里：
> **凡是"人定的权重"和"定性的帽子"，都留在展示层，不许进语义层。**
>
> 它是"30 表 3000 列"场景下压低物化成本的有效手段，
> 但前提是那些指标**必须是确定性的**——
> 启发式打分只能放展示层，派生机制帮不上忙。
