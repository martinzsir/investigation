# lens 能力化改造实施方案

> 借鉴《设备接入》一文"统一对象 + 外设化三问 + 七步接入"的心智，对研判手段（lens）做四项改造。
> 设计前事实核查：`verify_lens_capability_design.py` **25/25 通过**。

---

## 〇、先说已达成什么（避免重复投资）

那篇文章的核心是**消灭 N×M 集成**：不写"雷达→系统""光电→系统"各自的适配代码，而是把设备翻译成统一对象，驱动写在两边都不改的地方。

**lens 已经做到了这一跃**：

| 文章主张 | lens 现状（实测） |
|---|---|
| 翻译成统一对象是命根子 | `LensFact` 统一事实对象 `lens/type/strength/basis/evidence` |
| 驱动写在两边都不改的地方 | `prioritize_clues` 按 `dimension.source` 声明式派发，加 lens 不改内核 |
| 接入 ≠ 改硬件 | 4 个插件装载后，7 个事实消费方**无一按 lens 分支** |

所以本方案**不是重做**，是补上"设备侧没答完的三问"：你是什么（半答）、你的观测什么含义（未答）、你能执行什么（未答）。

---

## 一、现状核查（25 项实测）

### 1.1 声明面：10 个顶层字段，无一条能力信息

```
lens.json 已知顶层字段（10）：
base_jian / bindings / contributes_cross_level / criteria /
description / dimensions / id / name / version / views
```

`capability`、`kind`、`timeout`、`requires`、`depends_on`、`prepare`、`fact_types`、`scope` **全部缺失**。
未知键进 `extra` 透传不校验 —— **拼错静默**。

### 1.2 fact type：9 个自由字符串，无注册表

实测各 lens 产出的 type 字面：

```
利益重叠 周期时点 时序突变 时点集中 时窗贴近
枢纽集中 资金闭环 过桥链路 重复共现
```

`grep FACT_TYPE / fact_types / KIND_REGISTRY` → **0 处命中**。
且 `type` 与 `criteria` 声明名同源（"过桥链路"既是判据名也是 fact type），**没有独立的机器可读 id**。

### 1.3 调度面：ctx 只有三键

```python
ctx = {"assumption_confidence": ..., "jian_weight": ..., "time_semantics": ...}
```

无 store、无其他线索、无 prepare 批量钩子、无 timeout、无 lens 间依赖声明。

### 1.4 失败语义：一个 lens 坏 = 整批崩（硬失败）

```python
missing = validate_lenses(lens_specs)
if missing:
    raise LensError("已启用的 lens 存在未注册的计分源或未实现的判据，拒绝带病计分")
```

且 `_eval` 内算子调用**无 try 包裹**（实测 try 出现 0 次）—— 运行时异常同样向上传播。

### 1.5 启用语义：二元开关，无灰度

```
lenses.json: {"enabled": ["five_jian", "relation"]}
已安装 4 个：five_jian / fund_flow_structure / relation / temporal
未启用 2 个：fund_flow_structure、temporal   ← 影子模式的天然受体
```

### 1.6 下发通道：已有可扩展载体

```ts
interface LensDecl { id; name; version?; criteria?; contributes_cross_level? }
```

已随 `ontology-config` 下发，可直接扩展承载新字段，**无需新建通道**。

---

## 二、改造方案（2.8 人日）

### P0-1　fact type 注册表 + 输出 schema 校验　0.5 人日

**改什么**：给 `lens.json` 加 `fact_types` 声明，装载期与产出期双向校验。

```jsonc
// ontology/lens/relation/lens.json
{
  "id": "relation",
  "fact_types": [
    {"id": "chain",      "name": "过桥链路", "shape": "path",   "strength": "ratio"},
    {"id": "cycle",      "name": "资金闭环", "shape": "path",   "strength": "ratio"},
    {"id": "hub",        "name": "枢纽集中", "shape": "node",   "strength": "ratio"},
    {"id": "overlap",    "name": "利益重叠", "shape": "node",   "strength": "ratio"},
    {"id": "recurrence", "name": "重复共现", "shape": "pair",   "strength": "ratio"}
  ]
}
```

`type` 字段从**中文展示名**改为**稳定机器 id**，`name` 保留展示。
产出时校验：`type` 必须在注册表内、`strength` 符合声明量纲、`evidence` 必须含 `shape` 对应字段（path 型必须有 `path`）。

**为什么要 id 而非沿用中文名**：中文名会随业务措辞调整（"过桥链路"可能改成"桥接链路"），改名会让所有下游断言、举证模板、跨案统计一起断。id 是契约，name 是皮肤。

**校验是双向的**：
- 装载期：注册表内每个 id 必须在 `criteria` 有对应实现
- 产出期：每条 fact 的 `type` 必须在注册表内

**对应文章**：*"转换层同时也是校验层 —— 把经纬度写成度分秒字符串的字段，在这里就被拦下。"*

---

### P0-2　能力清单 + 调度（prepare 钩子与超时）　1.0 人日

**改什么**：给 `lens.json` 加 `capability` 声明，`lineage` 按声明调度。

```jsonc
{
  "capability": {
    "scope": "single",          // single | batch
    "needs": ["source_rows"],   // 需要的输入
    "budget_ms": 200,           // 单条线索预算
    "prepare": null,            // 批量预计算钩子名（batch 型必填）
    "parallel_safe": true
  }
}
```

三种 scope：

| scope | 含义 | 谁用 |
|---|---|---|
| `single` | 只看当前线索 | 现有 4 个 lens 全部 |
| `batch` | 需要跨线索/全图上下文 | 未来视频研判、团伙识别 |
| `global` | 需要全库统计 | 未来异常基线 |

**`prepare(clues, ctx) -> shared` 钩子**：`lineage` 在逐条计分**之前**调一次，产出 shared 上下文传给所有单条算子。

```python
shared = {}
for spec in lens_specs:
    if spec.capability.scope == "batch":
        shared[spec.id] = spec.prepare_fn(clues, ctx)   # 全批只算一次
```

**为什么不是直接把 `clues` 塞进 ctx**（这点很重要）：
- 塞 ctx → 算子退化成 O(N²)，每条线索各建一次图
- `prepare` → 全局图**只建一次**，单条算子签名不变，**向后兼容现有 4 个 lens**

这正是上一轮容量评估里"2000 万边建图 504 秒"的救法：建一次，而非 N 次。

**超时**：`budget_ms` 超预算 → 该 lens 降级为 `status.degraded`，不拖垮全批（与 P0-3 联动）。

**对应文章**：*"没有这条声明，任务编排层面对一堆黑盒设备，只能靠人工配置表猜测谁能干什么。"*

---

### P0-3　单 lens 失败隔离　0.5 人日

**改什么**：把一个 lens 的失败，从"整批崩"改成"该 lens 缺席、其余照常"。

现在的行为：

```python
missing = validate_lenses(lens_specs)
if missing:
    raise LensError(...)   # temporal 坏 → relation 的事实也一起没了
```

改造后：

```python
degraded: dict[str, str] = {}
for spec in lens_specs:
    try:
        load_lens_sources(spec)
        miss = validate_lenses([spec])
        if miss:
            raise LensError("；".join(miss))
    except Exception as e:
        degraded[spec.id] = f"{type(e).__name__}: {e}"
        lens_specs.remove(spec)      # 剔除，其余继续
```

运行时同理，`_eval` 内每个 `ev_fn` 单独 try：

```python
try:
    det = ev_fn(c, d, ctx)
except Exception as e:
    lens_status[d_lens] = {"degraded": True, "reason": str(e)}
    continue      # 该维度按 0 计分，但其他 lens 事实照常产出
```

**降级信息必须可见**：写进 `lens_status[lens_id].degraded`，前端已有的"不适用灰标签"机制（`lens.ts:58` 的 `isLensDegraded`）**直接复用，零新增 UI**。

⚠️ **保留一条硬失败红线**：若失败的是 `five_jian`（交叉等级唯一来源），或所有 lens 全部降级 → 仍 `raise`。
理由：事实少一两条可以继续办案；**定性等级没人算 = 整批结论不可信，必须停**。

---

### P1-1　影子模式（试点验证）　0.8 人日

**改什么**：`lenses.json` 的 `enabled` 从二元扩为三档。

```jsonc
{
  "enabled": ["five_jian", "relation"],
  "shadow":  ["temporal", "video_analytic"]   // 跑但不参与排序
}
```

| 档 | 行为 | 前端 |
|---|---|---|
| `enabled` | 参与计分与排序 | 正常 chip |
| `shadow` | 只产出事实，**不进权重、不影响排序** | 半透明 chip + "试用中"标记 |
| 未列 | 完全不跑 | 不显示 |

影子期记录每条事实，分析师核对后决定是否转正。
文章第 6 步：*"验收口径是误报率压到每 24 小时一次以下，不达标就退回第 3 步。"*

**为什么需要它**：现在装一个 lens 是**一次性豪赌** —— 装上就全量参与排序，错了会污染所有线索优先级，且**没有任何回看手段**。侦查场景里，一次误判的代价远大于晚几天上线。

---

## 三、影响分析

### 3.1 代码影响

| 文件 | 改动 | 风险 |
|---|---|---|
| `core/lens.py` | `known_top` 加 2 字段；`fact_types`/`capability` 解析与校验；`load_lens_sources` 支持 prepare | 中（装载核心） |
| `core/lineage.py` | 剔除式装载；`prepare` 钩子调用；`_eval` 加隔离与超时 | **高**（计分核心） |
| `ontology/lens/*/lens.json`（4 个） | 补 `fact_types`、`capability` | 低 |
| `ontology/lens/*/sources.py`（4 个） | `type` 中文名 → id | 中（**需同步改前端与测试**） |
| `frontend/.../ontologyConfig.ts` | `LensDecl` 扩展 | 低 |
| `frontend/.../domain/lens.ts` | type → 展示名映射 | 低 |

**最高风险在 `lineage.py`**：它是计分唯一核心路径，且**沙盒 duckdb 装不上，我无法实跑**。必须本地回归。

### 3.2 数据影响

- **objects/links/rules 等 22 个配置：零影响** —— 本次不改本体
- **lens 插件 4 个全要改**：`type` 从中文名改 id，涉及 9 个 fact type
- **存量案件快照**：装载时 `type` 若不在注册表 → 按 P0-3 降级为 degraded，**不会崩**
- **`lenses.json`**：新增 `shadow` 键（可选，缺省空数组）

### 3.3 测试影响

- `type` 改名会让**按中文名断言的测试失败** —— 需全量 grep 同步
- `validate_lenses` 硬失败改部分降级 → **断言"抛异常"的测试要改成断言"降级"**
- 核心套件 2 条既有失败（`TestRulebook` 缺 `self.fx`、P2-5 权重 0.688 vs 0.713）**不受影响**

### 3.4 前端影响

- `LensDecl` 扩展 → 契约向后兼容（新字段可选）
- 影子 chip 需新样式（半透明 + "试用中"）
- **降级展示零新增**：复用 `isLensDegraded`（`lens.ts:58`）

### 3.5 快照与指纹

lens 定义在上游层，**改 `lens.json` 会触发指纹变化**（P0 已加），提案需重评 —— 这是**正确行为**，不是副作用。

### 3.6 风险登记

| 风险 | 应对 |
|---|---|
| `lineage.py` 改动引入计分回归 | 保留 `score_formula` 自审计；对比改造前后逐位一致 |
| fact type 改名遗漏 | 双向校验（注册表↔产出）会在装载期硬失败 |
| prepare 钩子被滥用为 O(N²) | `scope: batch` 强制声明+预算，超限降级 |
| 影子期事实污染举证清单 | 影子事实打 `shadow: true`，导出举证时默认排除 |

---

## 四、业务价值分析

### 4.1 直接价值

**① 新研判手段从"改代码"变成"放配置"**

现在加一个 lens 已经不用改内核，但**加"跨线索"类手段必须改内核**（ctx 只有三键）。P0-2 之后，视频研判、团伙识别这类能力也能纯配置接入。

按上一轮评估，视频流接入 6.0 人日 —— 其中相当部分是为了绕开"ctx 看不见全批"这个限制。`prepare` 钩子（1.0 人日）能直接降低这部分成本。

**② 一个手段出错，不再停摆整个专案**

现在 temporal 的 sources.py 有一个 bug，**所有线索的事实全部消失**。
改造后：temporal 缺席，relation 与五间的事实照常产出，界面标注"时间研判暂不可用"。

侦查业务里，**少看一个维度可以继续办案，全批失败就停摆** —— 这个差异是决定性的。

**③ 事实从"给人看的字符串"变成"可计算的对象"**

type 有注册表之后，才能做：
- 跨案统计"过桥链路事实在多少案件中出现"
- 举证模板按 fact type 自动套用
- 两个 lens 产出同类事实时**系统能识别**（现在会当成两件事）

### 4.2 与文章理念的对应价值

| 文章 | 本方案 | 价值 |
|---|---|---|
| 统一对象 | 已达成 | N×M → N+M |
| 你是什么 | P0-2 能力清单 | 调度器不再面对黑盒 |
| 观测什么含义 | P0-1 注册表 | 融合有前提 |
| 能执行什么 | P0-2 prepare + 预算 | 能力可声明、可编排 |
| 断连重连 | P0-3 隔离 | 单点故障不扩散 |
| 小范围试点 | P1-1 影子 | 装手段从豪赌变验证 |

### 4.3 长期价值：为视频/互联网数据流铺路

上一轮评估的两条新能力，都依赖本方案：

- **视频流**：跨帧、跨线索研判 → 需要 `scope: batch` + `prepare`
- **互联网数据流**：新 lens 误报率未知 → 需要影子模式先跑再转正

**没有 P0-2，视频 lens 会被迫去改内核**，破坏"加手段不改代码"这个核心承诺。

---

## 五、完整举例

### 例 1：fact type 撞名（P0-1 解决的问题）

**场景**：装上视频研判 lens 后，它产出一条事实描述"两人轨迹在多处重复出现"，开发随手写了 `type: "重复共现"` —— 与 relation lens 的"重复共现"**完全同名**。

**现状后果**：
```
[关系·重复共现 0.83]  「甲、乙」重复 7 次
[视频·重复共现 0.91]  「甲、乙」在多处轨迹重合
```
前端 chip 并排显示，**系统认为是两件不同的事，人看起来是同一件事**。
分析师无法判断该采信哪个，跨案统计时两条被分别计数。

**改造后**：
```
[关系·recurrence 0.83]  「甲、乙」重复 7 次
[视频·co_occurrence 0.91]  「甲、乙」在多处轨迹重合
```
id 不同 → 系统确知是两类观测；若视频 lens 想声明"我这其实就是 recurrence"，必须显式写 `same_as: "relation.recurrence"`，**融合成为显式决策而非意外撞名**。

### 例 2：团伙识别需要跨线索（P0-2 解决的问题）

**场景**：要加一个"团伙结构"lens，判据是"多个线索之间是否共享核心节点"。

**现状**：ctx 只有三键，算子**看不到其他线索**。只有两条路：
1. 改 `lineage` 把 `clues` 塞进 ctx → 改内核，且每条线索各建一次图（O(N²)）
2. 在算子里偷偷做全局缓存 → 绕过契约，隐患埋下来

**改造后**：声明 `scope: "batch"` + `prepare: "build_global_graph"`，
`lineage` 在逐条计分前调一次 `build_global_graph(clues)`，产出全图；
单条算子从 `shared["gang"]` 取图查询。

**性能对比**（按上一轮 2000 万边实测）：

| 方案 | 建图次数 | 耗时 |
|---|---|---|
| 塞 ctx（每条各建） | N 次 | 504s × N |
| `prepare`（只建一次） | 1 次 | **504s → 一次性** |

### 例 3：一个 lens 崩，全批停摆（P0-3 解决的问题）

**场景**：temporal 的 `sources.py` 被改出一处 `KeyError`（源数据缺 `date` 列）。

**现状**：
```
LensError: 已启用的 lens 存在...拒绝带病计分
→ 整批线索无分数、无事实、无排序
→ 专案当天无法推进
```

**改造后**：
```
[五间·观察] [关系·过桥链路 0.667] [关系·利益重叠 0.500]
ⓘ 时间研判暂不可用（KeyError: 'date'）
```
relation 与五间的事实完整保留，**专案照常推进**，界面明确告知降级。

### 例 4：影子模式验证新手段（P1-1 解决的问题）

**场景**：新装视频研判 lens，业务方想知道它到底准不准。

**现状**：只能 `enabled` 全量开 → 它立刻参与所有线索排序。
若误报率高，**所有线索优先级被污染**，且无法回看"哪些是它导致的"。

**改造后**：
```jsonc
{"enabled": ["five_jian", "relation"], "shadow": ["video_analytic"]}
```
跑一周，视频事实**只记录不参与排序**，界面半透明显示"试用中"。
分析师核对 200 条事实，确认误报率可接受 → 移入 `enabled` 转正。

**这正是文章第 6 步**：*"验收口径是误报率压到每 24 小时一次以下，不达标就退回第 3 步。"*

---

## 六、实施顺序与验收

| 顺序 | 项 | 人日 | 验收要点 |
|---|---|---|---|
| 1 | P0-3 失败隔离 | 0.5 | 单 lens 坏 → 其余事实仍在；five_jian 坏 → 仍硬失败 |
| 2 | P0-1 fact type 注册表 | 0.5 | 未知 type 装载期失败；改名不影响下游 |
| 3 | P0-2 能力清单 + prepare | 1.0 | batch 型全批只调一次 prepare；超预算降级 |
| 4 | P1-1 影子模式 | 0.8 | shadow 事实不进排序、不进举证导出 |

**为什么先做 P0-3**：它风险最低、价值最高，且**为后续三项提供安全网** —— 后面改 `lineage` 时若出错，至少不会全批崩。

### 验收标准

- 改造前后，**已启用组合下逐条线索的 `priority_score` 逐位一致**（不启新功能时行为不变）
- 4 个现有 lens 全部补声明后装载通过
- 人为让 temporal 抛异常 → relation 事实仍在（P0-3）
- 人为写未注册 type → 装载期硬失败（P0-1）
- batch 型 lens 的 prepare 全批调用次数 == 1（P0-2）

---

## 七、需要你定的三件事

**① fact type 用英文 id 还是保留中文？**
我倾向**英文 id + 中文 name**。中文名会随业务措辞调整，改名会让下游断言、举证模板、跨案统计一起断。但代价是 9 个 type 要一次性改名，且前端要加映射。

**② 失败隔离后，five_jian 崩溃要不要仍硬失败？**
我倾向**保留硬失败**（定性等级没人算 = 整批结论不可信）。但这与"隔离"原则有张力，需你确认。

**③ 影子模式的事实要不要进举证导出？**
我倾向**默认排除**（未经验证的事实不该出现在出庭材料里），但允许分析师手动勾选。

---

## ⚠️ 验证限制

- **duckdb 沙盒装不上** → `lineage.py` 的改动无法实跑，只有静态核查
- **前端未 `npm run build`** → `node_modules` 装不上
- 本方案全部结论基于 25 项静态核查，**未经运行时验证**
