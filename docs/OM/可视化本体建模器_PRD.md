# 可视化本体建模器（S2）落地版 PRD

| 项 | 内容 |
|---|---|
| 版本 | v1.1 |
| 状态 | 概念版已冻结；v1.1 按代码核查修订数据模型 / 命名 / 权限事实（2026-09-14，核查记录见各节「核查修正」标注） |
| 对应阶段 | 《可视化本体建模器_分阶段实施方案》S2（5.2 人日） |
| 前置依赖 | S0-1（\_industry 三层落地）、S1（本体管理器壳） |
| 关联 | 《本体管理子系统_需求清单与验收用例》REQ-04 / REQ-05 |

---

## 一、概念版（已冻结，不再变更）

| 项 | 内容 |
|---|---|
| 核心用户 | 已授权 `is_ontology_admin=1` 且 rank ≥ 偏将 的专职本体管理员 |
| 要解决的一件事 | 改一个属性要在几百行 `objects.json` 里肉眼定位、手敲字段名 |
| 产品形态 | Web（Vue 3 前端新增模块，无新增依赖） |
| 页面结构 | 对象建模器（三栏：对象列表 / 属性表格 / 对象关系图）+ 保存确认弹窗 |
| 最小可用版功能 | ① 对象与属性可视化建模 ② 数据元下拉绑定并自动继承 ③ 对象关系可视化 + 基数声明 |
| 本版本不做 | rules / policies / views 可视化、数据元本身的编辑、影响面分析、提案评审 |
| 商业模式 | 内部系统，不对外销售 |
| 技术前提 | 已有账号与密级体系；数据存案件快照 ontology JSON；G6 v5 已装 |

---

## 二、背景与问题定义

### 2.1 现状实测（写本 PRD 前核查的结论）

现有"本体编辑页面"本质是**美化过的 JSON 编辑器**：

| 页面 | 行数 | 实际交互 |
|---|---|---|
| `ModelDesignerView.vue` | 280 | 两个 tab（objects / links）各自的**整包 textarea** |
| `DataElementsView.vue` | 316 | 字典表格 + JSON 文本框 |
| `EtlPipelineView.vue` | 352 | 整份 bindings 的 textarea |
| `PolicyMaskingView.vue` | 373 | policies.json 的 textarea |
| `RuleWorkshopView.vue` | 440 | 规则列表 + JSON 文本框 |

**结论：不是"页面分散"的问题，是根本没有建模界面。**

### 2.2 用户痛点（原话转述）

> "关键现在的页面我只能是改 JSON 文件。我需要像 Ontology Manager 那样的可视化界面，降低整个建模的难度，而且整体的组织层次应该更加清晰。"

拆解为三个可设计的具体问题：

| # | 痛点 | 本 PRD 对应功能 |
|---|---|---|
| P1 | 改一个属性要在几百行 JSON 里肉眼定位 | F1 属性表格 |
| P2 | 要记住 `data_element` / `composite` / `pk` / `kind` / `name_property` 这些 key 名 | F1 下拉 + 勾选框 |
| P3 | `links.json` 手写，看不出对象间拓扑 | F3 关系图 |

### 2.3 可用资产（决定实现方式）

| 资产 | 说明 |
|---|---|
| 9 个 JSON Schema | `schemas/objects|links|actions|bindings|functions|policies|proposal|rules|views.schema.json` |
| G6 v5 | `@antv/g6 ^5.1.1` 已装，关系图可直接用 |

> ⚠️ **Schema 覆盖不全 + 前端往返丢字段风险**（核查发现）：
> 1. `objects.schema.json` 对象条目声明 7 个字段（name/title/pk/kind/name_property/properties/runtime），实际 `objects.json` 还有 `jian` / `jian_source` 未声明（`links.json` 同样带 `jian`/`jian_source`，文件级还有 `_note`）。
> 2. schema 本身 `additionalProperties: true`，**校验层不会丢字段**；真正的丢失风险在**前端类型化模型往返**。现存佐证：`ModelDesignerView` 的 TS 预检 `VALUE_TYPES` 只列 5 种（后端 9 种），且 `{data_element:...}` 形式的属性值会被误判非法。
>
> **本 PRD §7 R2 有针对性设计，这是硬性要求，不是可选项。**

---

## 三、范围

### 3.1 本版本做

| 编号 | 功能 | 说明 |
|---|---|---|
| F1 | 对象与属性可视化建模 | 对象列表增删改 + 属性表格增删改 |
| F2 | 数据元绑定 | 属性行下拉选择数据元，自动继承 format / mask / clean_rule |
| F3 | 对象关系可视化 | 关系图增删 link + one-to-many 基数声明 |

### 3.2 本版本明确不做

| 不做 | 原因 | 归属 |
|---|---|---|
| rules / functions 可视化 | 超出最小可用范围 | S3 |
| policies / actions 可视化 | 涉及红线门禁，需单独设计 | S4 |
| views 可视化 | 低频 | S4 |
| **数据元本身的增删改** | 数据元是独立文件（`data_elements.json`），不属于对象建模 | S3 |
| 影响面分析 | 依赖 S2–S4 建成的完整依赖图 | S5-3 |
| 提案评审流程 | 需真实协作需求 | S5-4 |
| `llm_policy.json` 任何开放 | 出网前的最后一道闸，不能交给被闸门管的人 | **永不开放** |

### 3.3 渐进替换策略（用户已确认）

- 新界面**不删除** `ModelDesignerView.vue`，后者保留为「JSON 编辑」入口
- 两者**读写同一份** `objects.json`（案件快照）
- **不做双向实时同步**：任一边保存后，另一边需重新载入
- 页面顶部常驻提示：「此对象也可在 模型设计器 → JSON 编辑 中修改，两边数据一致，切换后请重新载入」

---

## 四、术语

| 术语 | 定义 |
|---|---|
| 对象类型 | `objects.json` 中的一个条目，如 `person`；物化后的语义层表名才是 `obj_person`（`obj_` 是表名前缀，不属于对象名；name 模式 `^[a-z_]+$`） |
| 属性 | 对象类型 `properties` **dict** 中的一个键，如 `id_card`；值为值类型字符串，或 `{type, composite, data_element}` 映射 |
| 数据元 | `_shared`（全域）/ 行业层（S0-1 未完成暂缺）/ 案件层 `data_elements.json` 中的定义，如 `DE_IDCARD` |
| pk | 主键，**对象级字段**（字符串，取已声明属性名） |
| link | `links.json` 中的对象间关系，字段为 `name` / `title` / `from_obj` / `to_obj` |
| 基数 | link 的端点数量约束，如 one-to-many；当前 schema 未声明，F3 新增 |
| jian | 五间归属（内间/因间/反间/死间/生间），`objects.json` 与 `links.json` 中均存在的未声明业务字段（配套 `jian_source`） |
| title | 对象/链接的显示名字段（注意：代码与实际 JSON 用 `title`，**不存在** `label` / `description` 字段） |

---

## 五、页面结构与导航

### 5.1 入口

```
本体管理器（S1 提供的独立 App）
   └─ 左树：本体文件树（20 个文件，标注可写状态与来源层）
        └─ 点击 objects.json
             └─ 右区：对象建模器（本 PRD）
```

> 说明：本体管理器是**独立 App**，不是第七个侧栏组。理由：本体管理是**配置态**而非作业态，低频且危险，不该混进日常办案导航。

### 5.2 三栏布局

```
┌─────────────┬─────────────────────────────────────┬──────────────────┐
│  对象列表    │      属性表格                        │   对象关系图      │
│  (左, 280px)│      (中, flex)                      │   (右, 360px)    │
│             │                                     │                  │
│ ● person    │ 属性名│类型│数据元│复合│pk│敏感      │   [G6 关系图]     │
│ ○ account   │ ──────────────────────────────────  │                  │
│ ○ transaction│ raw_name│ str │  -    │ □ │ ▶ │ - │   person ──┐      │
│             │  id_card │ str │DE_IDCARD│ □ │ □ │🔒│        └──> account│
│             │  phone   │ str │DE_PHONE │ □ │ □ │🔒│                  │
│ [+ 新建对象] │  [+ 新增属性]                        │   [+ 新建关系]    │
└─────────────┴─────────────────────────────────────┴──────────────────┘
                          [保存]  [放弃更改]
```

### 5.3 顶部固定区（跨三栏）

| 元素 | 内容 |
|---|---|
| 面包屑 | 本体管理器 / objects.json / person |
| 来源层标签 | `[案件层]` / `[金融行业层]` / `[全域层]` |
| 未知字段提示 | 「本对象有 N 个界面未覆盖字段，已原样保留（可切 JSON 页编辑）」（仅当 N>0） |
| 脏标记 | 有未保存更改时显示 `● 未保存` |

---

## 六、功能详情

### F1 对象与属性可视化建模

#### F1.1 对象列表（左栏）

**字段**

| 列 | 来源 | 规则 |
|---|---|---|
| 对象名 | `name` | 新建后**不可改**（见 §7 红线 R1）；模式 `^[a-z_]+$` |
| 显示名 | `title` | 可编辑（字段名是 `title`，不是 label） |
| 属性数 | `properties` 键数 | 只读 |
| kind | `kind` | 只读列（entity/event），编辑入口在 F1.3 |
| 来源层 | 派生 | 只读 |

**操作**

- 新建对象：输入框填 `name`（必填、全局唯一、`^[a-z_]+$`）、`title` 选填、`kind` 必选（entity/event）；`pk` 与 `name_property` 是 schema required，允许建后再补，但保存前必须齐备
- 删除对象：二次确认；**若被 link 引用则阻止**（见 F3 异常 E3-2）
- 切换对象：有未保存更改时提示先保存或放弃

#### F1.2 属性表格（中栏）★ 最高价值

**数据结构（核查修正）**

`properties` 是 **dict**（属性名 → 值），不是数组；每键的值二选一：

```jsonc
"properties": {
  "raw_name": "string",                          // 形式一：纯值类型字符串
  "phone": {"data_element": "DE_PHONE"},         // 形式二：映射，键仅 type / composite / data_element
  "cp_info": {"type": "string", "composite": true}
}
```

**表格列（UI 投影，落盘仍是上述 dict）**

| 列 | 对应 JSON | 控件 | 校验 |
|---|---|---|---|
| 属性名 | `properties` 的键 | 文本框 | 必填；对象内唯一（dict 键重复会被 JSON 解析静默覆盖，新增行时必须在 UI 层拦截，见 E1-1） |
| 类型 | 值类型 或 `{type}` | **下拉**（`string / integer / decimal / date / boolean / timestamp / duration_days / enum / json`，与 `core/ontology.py TYPE_NAMES` 一致） | 绑定数据元时可省略（随数据元 type）；单独给出时必填 |
| 数据元绑定 | `{data_element}` | **下拉**（见 F2） | 选填 |
| 复合列 | `{composite}` | **勾选框** | 选填；复合整列降级路径（实例：reqd_case `cp_whole.cp_info`） |
| pk | 对象级 `pk` 字段 | **单选勾选框**（同对象仅一个，与 F1.3 pk 下拉联动） | — |
| 敏感 | —（不在 objects.json） | **只读回显**：绑定数据元 `sensitive:true` 时显示 🔒 标记 | 编辑走数据元层与 `policies.json` 属性级遮蔽，此处不可改 |
| 操作 | — | 删除按钮 | 见异常 E1-2 |

> ⚠️ **核查修正**：实际数据**没有** per-property `label` / `sensitive` 字段——显示名只有对象级 `title`；敏感性属于数据元（`data_elements.json` 的 `sensitive` / `mask`）与 `policies.json` 遮蔽策略。v1.0 的「显示名 / 敏感」可编辑列已删除，避免造出无处落盘的假字段。

**属性结构说明（核查结论）**

属性值的 `oneOf` 只有 3 个维度：类型 / 是否复合 / 数据元绑定。结构扁平，因此表格能完整表达，**不需要嵌套编辑器**。这是本功能能压到 1.5 人日的原因。

#### F1.3 对象元信息（中栏上方折叠区）

| 字段 | 控件 |
|---|---|
| `title` | 文本框（显示名；实际数据无 `description` 字段，文件级注释走 objects.json 顶层 `_note`，不在对象编辑范围） |
| `kind` | 下拉（entity / event；决定代理键分配方式：entity 按 name_property 值、event 按行——改动属结构性） |
| `name_property` | 下拉（从已声明属性中选；schema required，event 仅作列序） |
| `jian` / `jian_source` | 下拉五间 / 文本框（未声明字段，编辑后同样原样保留，见 R2） |
| `pk` | 下拉（从已有属性中选，与表格 pk 列联动） |

### F2 数据元绑定

**交互**

1. 属性行「数据元」列点击下拉
2. 下拉列表按**来源层分组**展示：

```
全域层
  DE_IDCARD    身份证
  DE_PHONE     手机号
金融行业层
  DE_BANK_NO   银行卡号
本案件层
  DE_CASE_X    自定义
```

3. 选中后，**自动继承并显示**该数据元的 `format` / `mask` / `clean_rule`（只读回显，不可在此编辑）

**写入格式**

```jsonc
// 选中 DE_IDCARD 后，properties dict 中该键落盘为
"id_card": {"data_element": "DE_IDCARD"}
// type 可省略（实测 reqd_case 9 处绑定均省略，列类型随数据元 type）；
// 若同时给出 type，必须是 TYPE_NAMES 合法值
```

> 实测依据：reqd_case 包中已有 9 处此类绑定（person.phone / person.id_card / transaction.amount / transaction.date / call.phone_raw / call.date / case_ledger.case_type / case_ledger.case_date / cp_split.cp_idcard），格式确认如上。

**取消绑定**：下拉选「（无）」，清除 `data_element` 键

### F3 对象关系可视化

**交互**

- 右栏 G6 关系图展示当前案件所有对象类型及其 link
- 从对象节点拖出连线 → 弹窗填写 `name` / `title` / `from_obj` / `to_obj` / **基数**
- 点击已有连线 → 编辑或删除
- link 级 `jian` / `jian_source` 等未声明字段同样按 R2 原样保留（default 包 links.json 已有「反间」实例）

**基数（cardinality）字段**

> **决策依据**：`links.json` 目前只有端点，无基数。不加则关系图只能表达"有无关系"，表达不了一对多——而关系图是 S2 的核心卖点，省这 0.2 人日不划算。
>
> **落地配套**：cardinality 是新增声明字段，须同步修订 `schemas/links.schema.json`（现行 `additionalProperties: true` 不会拦截未声明字段，但 schema 是声明性文档，必须同步，否则声明与实际漂移）。

| 值 | 含义 |
|---|---|
| `one_to_one` | 一对一 |
| `one_to_many` | 一对多 |
| `many_to_many` | 多对多 |
| （空） | 未声明，兼容历史数据 |

**布局**

复用《画布布局技术评估与演进方案》的确定性结论：**不使用 force / dagre 等不可复现布局**，采用固定分层 + 确定性排序，保证同一份数据每次打开位置一致。

---

## 七、红线与硬约束（源自核查，非偏好）

### R1：`name` 新建后不可改

`name` 被 `bindings.json`（列映射）、`pk`、`links.json`（端点）引用。**允许改名 = 隐式断链，且不会报错**。

> 静默的引用断裂在取证系统里最危险。因此定死：不可改，要改名就新建再迁。

UI 表现：新建时 `name` 可输入；保存后该字段置灰，附 tooltip「对象名被 bindings / links 引用，不可修改。如需改名请新建对象并迁移数据。」

### R2：schema 未覆盖字段一律原样保留 + UI 显式提示 ★

**问题**（核查实测）：

- `objects.schema.json` 对象条目声明 7 字段，实际 `objects.json` 还有 `jian` / `jian_source` 未声明；`links.json` 同样带 `jian` / `jian_source`；文件级还有 `_note`
- 属性值 dict 形式携带 `data_element` / `composite`——按 v1.0「纯字符串类型」的类型化模型往返会把它们削掉
- schema 本身 `additionalProperties: true`，**校验层不丢字段**；丢失风险全部在**前端类型化模型往返**。现存佐证：`ModelDesignerView` 的 TS 预检 `VALUE_TYPES` 仅 5 种（后端 9 种）、`{data_element:...}` 属性值被误判非法——类型化校验一旦写窄，合法数据就被拦或被削

**要求**：

1. 保存时，界面未覆盖的字段（对象级 / link 级 / 文件级 `_note` / 属性级 dict 键）**原样回写**，不丢弃
2. 顶部固定区显示：「本对象有 N 个界面未覆盖字段，已原样保留（可切 JSON 页编辑）」
3. 验收用例 **UC-S2-3** 专门覆盖此条

### R3：绝不写"自动触发 RESCAN"

**问题**（核查实测）：`ConfigConfirmDialog` 对 `dangerous=true` 一律显示「保存后自动触发 RESCAN 重跑」，7 个页面都用它，但**只有 RuleWorkshop 真正入队**（`rule_workshop.py:191`）。

**这是当前正在产生错误后果的假承诺**——用户以为机器语义已更新，`obj_*` 表其实还是旧的。

本 PRD 的保存反馈按**改动分级**给文案，见 §8.3。

---

## 八、保存与校验

### 8.1 保存流程

```
点击 [保存]
   ↓
客户端校验（R1 唯一性 / 属性名唯一 / 必填 / 类型合法）
   ↓ 通过
危险确认弹窗（仅当改动属"结构性改动"）
   ↓ 填写理由（必填）
服务端写入 → _validate_in_temp（临时目录试写 + load_pack 校验）
   ↓ 通过
落盘 + re-fingerprint（本体版本 +1，追加版本历史）
   ↓
审计留痕（操作人 / 理由 / 变更文件 / 本体版本）
   ↓
按 §8.3 显示提示
```

> `_validate_in_temp` 是现有能力（`server/app/routers/model_designer.py`）：复制到临时目录 → 写入 → 跑 `load_pack` 校验 → 通过才落盘。**必须复用，不要绕开。**
>
> ⚠️ **核查修正**：现状 PUT 为整包覆盖、**无乐观锁**；E1-4 的 version 比对是 S2 需**新增**的服务端能力（响应中已有 `data_version` 可作比对基础，但服务端目前不校验）。

### 8.2 改动分级

| 级别 | 改动 | 危险确认 | 理由必填 |
|---|---|---|---|
| **结构性** | 新增/删除对象类型、改 `pk` / `kind` / `name_property`、改属性 `type` / `composite` | ✅ | ✅ |
| 语义性 | 改 `title` / `jian` / `jian_source`、改数据元绑定 | ❌ | ❌ |
| 关系性 | 新增/删除/修改 link | ✅ | ✅ |

### 8.3 保存后提示文案（按级别）

| 级别 | 文案 |
|---|---|
| **结构性** | 🔴「本体已保存。**需重跑 BUILD 才生效** —— 新增对象类型不重跑会导致 `obj_*` 表不存在，相关功能不可用。」<br>按钮：`[前往任务中心重跑]` / `[稍后自行处理]` |
| 语义性 | 🟡「已保存，下次 BUILD 后生效。」 |
| 关系性 | 🔴「关系已保存。**需重跑 BUILD 才生效**。」<br>按钮：`[前往任务中心重跑]` / `[稍后自行处理]` |

> **绝不出现"已自动触发"字样**，除非该功能确实入队（当前无）。

---

## 九、状态机

### 9.1 页面级

| 状态 | 触发 | 表现 |
|---|---|---|
| `loading` | 进入页面 | 三栏骨架屏 |
| `empty` | 无对象类型 | 空态：「尚未定义对象类型」+ `[新建对象]` |
| `ready` | 载入完成 | 正常三栏 |
| `dirty` | 任一编辑 | 顶部 `● 未保存`，保存按钮高亮 |
| `validating` | 点保存 | 保存按钮 loading，禁用 |
| `saving` | 校验通过 | 弹窗或按钮 loading |
| `saved` | 落盘成功 | 提示 + 脏标记清除 |
| `failed` | 服务端拒绝 | 错误条 + 保留用户输入 |
| `conflict` | 服务端 version ≠ 本地载入时 | 见 E1-4 |

### 9.2 对象级

| 状态 | 标记 |
|---|---|
| `persisted` | 无标记 |
| `new` | 左侧 `新增` 徽标 |
| `modified` | 左侧 `已改` 徽标 |
| `deleted` | 灰显 + 删除线，保存时移除 |

### 9.3 关系级

| 状态 | 标记 |
|---|---|
| `persisted` | 实线 |
| `new` | 虚线 + 绿色 |
| `deleted` | 虚线 + 红色删除线 |

---

## 十、异常与边界处理

| 编号 | 场景 | 处理 | 文案 |
|---|---|---|---|
| E1-1 | 属性名对象内重复 | 阻止保存，定位到行（dict 键重复会被 JSON 解析静默覆盖，必须在 UI 层拦截） | 「属性名 `{name}` 重复，请修改」 |
| E1-2 | 删除作为 `pk` 的属性 | **阻止** | 「`{name}` 是主键，不可删除。如需更换请先指定其他属性为主键」 |
| E1-3 | 对象 `name` 全局重复 | 阻止 | 「对象名 `{name}` 已存在」 |
| E1-4 | 并发冲突（他人已改） | 提示，**不覆盖**（现状无 version 校验，需 S2 新增服务端比对，`data_version` 已具备） | 「本体已被他人修改（当前版本 v{N}）。请重新载入后再编辑」<br>按钮：`[重新载入]`（丢弃本地更改）/ `[在新窗口查看差异]` |
| E2-1 | 数据元被删除/不存在 | 降级为自由类型 + 提示 | 「绑定的数据元 `{DE}` 不存在，已降级为普通类型，请重新选择」 |
| E2-2 | 数据元与属性 `type` 冲突 | 警告，**不阻止** | 「数据元 `{DE}` 的推荐类型为 `{T1}`，当前为 `{T2}`」 |
| E3-1 | 连线端点对象不存在 | 阻止 | 「端点对象 `{name}` 不存在」 |
| E3-2 | 删除被 link 引用的对象 | **阻止**并列出引用 | 「对象 `{name}` 被 {N} 条关系引用，不可删除。引用关系：`{link1}`、`{link2}`…」 |
| E3-3 | 基数与实际数据冲突 | **警告，不硬失败** | 「声明为 `{cardinality}`，检测到 {N} 条数据不符合」 |

> **E3-3 设计说明**：取证数据常脏，不该因声明不符就废掉整个本体。故取警告而非硬失败。

---

## 十一、权限

| 动作 | 要求 |
|---|---|
| 进入对象建模器 | 案件可访问（现有案件权限） |
| 保存结构性改动 | `is_ontology_admin=1` **且** rank ≥ 偏将 |
| 保存语义性改动 | 同上（不做二次分级，避免复杂度） |
| 编辑全域层 / 行业层 | 走标准域写路由（REQ-07，S0-1 提供） |

> **核查修正**：`is_ontology_admin` 账号字段当前**不存在**，写门禁现状只有 `require_analyst`（clearance≥2 或 human/system，见 `server/app/snapshot_config.py`）。双条件门禁是 S2 的**新建能力**：需新增账号标记字段 + 保存路由双条件校验，不是复用现成开关。
>
> **双条件门禁的意义**：光是本体管理员不够（还得有办案资格），光是偏将也不够（不能改标准）。与 `core/policy.py:95` 既有模式（`role in roles and clearance >= min_clearance`）同构。

---

## 十二、验收用例（每条可独立跑）

| 编号 | 用例 | 预期 | FAIL 判据 |
|---|---|---|---|
| UC-01-1 | 新建对象 `test_object`（2 属性）→ 保存 | `objects.json` 结构正确，可被 `load_pack` 加载 | 加载失败或字段缺失 |
| UC-01-2 | 属性表格改 `type` string→date → 保存 | JSON 中 type 变更，保存后提示「需重跑 BUILD」 | 无 BUILD 提示 |
| UC-01-3 | 删除作为 `pk` 的属性 | **阻止**并提示属性名 | 删除成功 |
| UC-01-4 | 属性名填重复值 | 阻止，定位到行 | 保存成功 |
| UC-01-5 | 新建对象后点保存 | 出现危险确认弹窗，理由必填 | 无弹窗或理由可空 |
| UC-01-6 | 绑定 `DE_IDCARD` 的属性行查看「敏感」列 | 只读 🔒 回显，无编辑控件 | 出现可编辑的敏感勾选 |
| UC-02-1 | 属性绑定 `DE_IDCARD` | properties dict 该键落盘为 `{"data_element":"DE_IDCARD"}` | 格式不符或需手填 |
| UC-02-2 | 绑定后查看继承 | `format`/`mask`/`clean_rule` 回显 | 未回显 |
| UC-02-3 | 取消绑定 | `data_element` 键被移除 | 残留空值 |
| UC-02-4 | 绑定不存在的 `DE_XXX` | 降级为自由类型 + 提示 | 静默保存成功 |
| UC-03-1 | 关系图拖连线新建 link | `links.json` 同步含 cardinality，且 `links.schema.json` 已同步修订 | 缺失 cardinality 或 schema 未同步 |
| UC-03-2 | 删除被 link 引用的对象 | **阻止**并列出引用 | 删除成功 |
| UC-03-3 | 声明 one_to_many 但数据不符 | **警告不阻止** | 硬失败 |
| **UC-S2-3** | 修改含 `jian` 的对象 → 保存 | **对象级 `jian`/`jian_source`、文件级 `_note`、属性级 `{data_element, composite}` 均不丢失** ★ | 任一字段丢失 |
| UC-S2-6 | 在 ModelDesigner JSON 页改 → 回新界面 | 需重新载入才可见，数据一致 | 数据不一致或双向实时同步 |

> **UC-S2-3 是本 PRD 最重要的用例**——它专门防 §7 R2 描述的静默数据丢失。本条不过，整个 S2 不应发布。

---

## 十三、已定决策（不再变更）

| # | 决策 | 依据 |
|---|---|---|
| D1 | `name` 新建后不可改 | R1，防隐式断链 |
| D2 | 未覆盖字段（对象级 / link 级 / `_note` / 属性级 dict 键）原样保留 + UI 提示 | R2，防 `jian` 静默丢失；丢失风险在前端类型化往返而非 schema 校验 |
| D3 | 保存反馈不写"自动触发 RESCAN" | R3，修现有假承诺 |
| D4 | 渐进替换，保留 JSON 页 | 用户确认 |
| D5 | 基数冲突取警告不取硬失败 | 取证数据常脏 |
| D6 | 关系图使用确定性布局 | 复用《画布布局方案》结论 |

---

## 十四、待确认（实施前需明确）

| # | 事项 | 建议 |
|---|---|---|
| 1 | S0-1（`_industry` 三层）未完成前，F2 数据元下拉只能选到全域层与案件层 | 建议先完成 S0-1 再开 S2 |
| 2 | 属性表格是否支持拖拽排序 | 建议不支持（properties 为 dict，键序无强语义） |
| 3 | 是否需要批量导入属性（如从 CSV） | 建议不做，属 S3 |
| 4 | F1.2「敏感」列改为只读回显后，是否在行内提供跳转数据元页 / 权限遮蔽页的快捷入口 | 建议提供（替代 v1.0 误设的可编辑勾选） |
