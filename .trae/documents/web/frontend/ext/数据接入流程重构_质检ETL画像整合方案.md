# 数据接入流程重构：质检前移 + ETL 界面执行 + 画像消费

> 版本 **v1.2** ｜ 日期 2026-09-11
> 用户决策：① 质量需要前移，且后续过程中也可验证；② ETL 应在接入过程中发挥作用，明显质量问题或不符合数据元要求的可在界面执行 ETL 操作，尚不支持的提出建议；③ 数据画像在接入过程中未起作用；④ **数据元推荐位置太晚**；⑤ **数据元应是全域的，行业按金融/医疗划分**
> 依据：`investigation_v3` 全链路核查（ingest 三接口、source_analyze、quality 四扫描、etl 路由、clean_ops 11 op、列画像双实现、analyze 与 worker/recommend 重复计算、**双包数据元隔离实测**）
> **核心结论：数据元「从哪来」与「在哪用」是同一条链的两端，必须合并改造**
> **v1.1 变更：数据元推荐从 Step 4 前移到 Step 1**（它不只是"晚"，是**位置错了**——后续环节的判据输入，且**被重复计算两遍**）
> **v1.2 变更：合入《数据元全域化与行业分层实施方案》作为 §3.0 前置依赖层**
> 合并后本文档为数据元改造的**唯一方案入口**；原全域化文档保留作为历史决策留档

---

## 0. 结论先行

| 判断 | 结论 |
|---|---|
| 质检要前移吗？ | ✅ **要**，但**语义态质检必须保留**（双阶段，不是替换） |
| ETL 能在接入中执行吗？ | ✅ 能，但需先补 op 能力；且分 **A 一键 / B 预演确认 / C 仅建议** 三类 |
| 画像没起作用？ | ✅ 确认，**列画像已算但前端零消费**（后端算好 4 字段，向导只显示 4 列元数据） |
| 最关键的成本洼地 | 列画像展示只要 **0.3 人日**（数据现成，纯前端接线） |
| 最大前置缺口 | 🔴 **无拆分 op**（`split_op` 是解析 token 的函数，不是拆分操作） |
| **v1.1 新增：数据元推荐位置** | 🔴 **不只是晚，是位置错了** —— 它是 Step2 质检/处置的**判据输入**，却排在队尾；且 Step1 已同步算好被丢弃，Step4 异步重算（**算了两遍**） |

---

## 1. 现状：接入流程与三个断点

### 1.1 现有接入四步

```
Step 0 上传   → POST /cases/{cid}/sources/upload
                暂存 + 指纹 + ingest_io.profile_columns 列画像
Step 1 分析   → POST /cases/{cid}/sources/{uid}/analyze
                source_analyze.analyze_source（_column_profile + 建议）
Step 2 确认   → POST /cases/{cid}/sources/{uid}/import
                幂等判定 + validate_mapping → 入队 TASK_IMPORT
Step 3 完成   → 生成数据元推荐（可选按钮 → 跳 /c/suggest）
```

随后 BUILD 物化，质检在 BUILD **之后**手动触发。

### 1.2 四个断点

| # | 断点 | 证据 |
|---|---|---|
| 1 | **画像算了不给看** | `source_analyze._column_profile` 产出 `inferred_type`/`null_rate`/`distinct`/`samples`；`IngestView.vue` **四字段零引用**，映射表只有"声明列/必选/匹配/映射到上传列" |
| 2 | **质检在 BUILD 后** | `handle_quality`：`if ver < 1: raise NO_VERSION`；四扫描全部依赖 `OntologyReadGateway`（读 `obj_*`） |
| 3 | **ETL 是编辑视图** | `_pipeline_from_bindings` 仅把 bindings 转成可编辑字段；`composite_props: []`（复合列无位置）；`validate` 的 A/B 出路**只是两个标签，无实现** |
| 4 | 🔴 **数据元推荐位置错了**（v1.1） | 见 §1.4 |

### 1.4 🔴 数据元推荐：不只晚，而且算了两遍（v1.1 新增）

#### 决定性证据：Step 1 已同步算好，前端没用

`analyze` 接口（Step 1，**同步**）返回：

```python
return {"columns": columns, "declared_tables": tables,
        "suggestion": suggestion, "element_hints": element_hints}
```

而 Step 4 的异步任务 `worker/recommend.py` 做的是**完全一样的事**：

| 步骤 | analyze（Step1 同步） | worker/recommend（Step4 异步） |
|---|---|---|
| 加载数据元 | `load_data_elements` | `load_data_elements` |
| 计算 | `recommend_for_table(...)` | `recommend_for_table(...)` |
| 过滤 | `next(x for x in recs if x.get("data_element"))` | **同一行，一模一样** |
| 输出 | `element_id`/`name`/`confidence`/`evidence` | **同四个字段** |

**同一份计算做了两遍，第一遍结果被丢弃，第二遍在四步之后才给用户看。**

前端 TS 类型 `element_hints?: unknown[]`（`domain/mapping.ts:53`）有定义，**但无组件渲染**。

#### 为什么必须提前：它是后续环节的判据输入

| 后续环节 | 依赖数据元的什么 |
|---|---|
| Step2 接入态质检 | `format` / `enum`（判断"不符合要求"） |
| Step2 ETL 处置 | `clean_rule`（该用哪个 op 洗） |
| 遮蔽 | `sensitive` / `mask` |

**Step 4 才推荐 → Step 2 的质检与处置没有判据 → 死锁。**

> 这正是前几轮连续发现"质检游离""ETL 没作用""数据元没作用"的共同根因——
> **它们缺的是同一个东西，而它排在队伍最后**。

#### 正确位置：Step 1，与列画像并排

映射的本质是三件事，应一起决定：

```
源列 → 目标属性   （映射，已有）
     → 语义标准   （数据元，★ 挪到 Step1）
     → 清洗规则   （clean_rule，由数据元派生）
```

### 1.3 为什么质检前移是"新入口"而非改参数

四扫描各 5 处 `gateway` 引用，输入是**物化后的 `obj_*` 表**。
前移到接入态 = 对**原始暂存件 DataFrame** 扫描，是**新的扫描入口**。

---

## 2. 更新后的数据接入流程（核心交付）

### 2.1 新流程：六步

```
Step 0  上传文件
        ↓ 暂存 + 指纹 + 列画像（已有）
Step 1  列分析与映射
        ├ 列画像（样本/空值率/推断类型）      ★ 新
        ├ 匹配建议（已有）
        └ 数据元推荐 + 采纳                   ★ v1.1：从 Step4 前移
        ↓ 采纳 → 写 bindings.data_element
Step 2  【新】质量预览与处置   ★ 用 Step1 采纳的数据元作判据（format/enum/clean_rule）
        ↓                     ★ 处置写 config 草稿（不自动生效）
Step 3  确认导入
        ↓ 入队 TASK_IMPORT
Step 4  完成                  ★ v1.1：删除异步推荐任务（去重）
Step 5  BUILD → 语义态质检    ★ 保留（原有能力，自动触发）
                              ★ compliance/unit_scan 因 Step1 采纳而真正生效
```

### 2.2 每步详述

| 步骤 | 页面/接口 | 用户可见 | 状态分支 |
|---|---|---|---|
| **0 上传** | `/c/wizard` Step0 | 文件选择、格式提示 | 不支持格式 / 空文件 / 解析失败 |
| **1 分析与映射** | Step1 | **声明列 + 匹配 + 样本 + 空值率 + 推断类型 + 数据元推荐** | 缺必选列（降级警告不阻断）/ 低置信琥珀高亮 / 数据元推荐为空（[待确认] 提示） |
| **2 质量预览与处置** | **Step2（新）** | 问题清单 + 逐条处置（判据来自 Step1 采纳的数据元） | 无问题直接下一步 / C 类仅建议 |
| **3 确认导入** | Step3 | 映射、数据元、处置汇总确认 | 权限不足（clearance≥2）/ 重复指纹 409 |
| **4 完成** | Step4 | 导入结果（**不再含元推荐**） | — |
| **5 BUILD** | 任务中心 | SSE 六阶段 | FAILED 重试 / PENDING 取消 |

### 2.3 关键形态：Step2 三分类处置

| 类 | 判据 | 界面形态 | 例子 |
|---|---|---|---|
| **A 一键应用** | op 存在 + 无损 | 「应用」按钮 | `strip_thousands`、`cn_date_norm`、`despace`、`to_upper` |
| **B 预演 + 确认** | op 存在 + 可能丢信息 | 「预演」→ 显示影响 N 行 → 确认 | `reject_if`、`digits_only`（账号去横杠） |
| **C 仅建议** | 无 op | 提示上游处理 + 复制建议 | **复合列拆分**、跨行去重、语义纠错 |

> **B 类是安全关键**：`reject_if` 会丢行，不能一键应用；必须"预演 → 看影响行数 → 确认"。

---

## 3.0 【v1.2 合入】数据元全域化与行业分层（前置依赖层）

> 本节原为独立方案《数据元全域化与行业分层实施方案》，现合入作为**前置层**。
> 原因：数据元「从哪来」（本节）决定「在哪用」（§3.1 起）——
> 接入态质检要读 `format`、ETL 处置要读 `clean_rule`、遮蔽要读 `sensitive`/`mask`，
> **这些字段由本节的三层结构定义**。

### 3.0.1 为什么全域化（收敛 vs 发散）

| | 本质 | 变化特性 |
|---|---|---|
| **数据元** | 标准（身份证 18 位、校验位、手机格式） | **收敛**，不因案件而变 |
| **本体包** | 业务建模（对象/链接/规则） | **发散**，每案不同 |

**把标准绑在业务包上，方向反了。**

### 3.0.2 🔴 实测：包隔离造成标准漂移

| 包 | 数据元 | `prop_data_elements` 引用数 |
|---|---|---|
| `default`（模板包） | DE_IDCARD/GENDER/CURRENCY/ID_TYPE/CASE_TYPE | **0** |
| `reqd_case`（案件包） | DE_IDCARD/PHONE/AMOUNT/DATE/CASE_TYPE | **10** ✅ |

```
两包交集:    {DE_IDCARD, DE_CASE_TYPE}
仅 default:  {DE_GENDER, DE_CURRENCY, DE_ID_TYPE}
仅 reqd_case:{DE_PHONE, DE_DATE, DE_AMOUNT}
```

**两个实质危害**：
1. **重复声明**——同一数据元各写一遍
2. **标准漂移** 🔴——两包 `format` 若不一致，同一字段在不同案件校验标准不同

> 这正是"在 default 补数据元却对 reqd_case 案件无效"的根因。

### 3.0.3 三层目录结构

```
ontology/                          ← base_dir（案件快照为 cases/{cid}/ontology）
├── _shared/
│   └── data_elements.json         ← 【全域基础层】跨行业通用
├── _industry/
│   ├── 金融/data_elements.json    ← 【行业层】SWIFT、银行账号、卡号…
│   ├── 医疗/data_elements.json    ← 病历号、ICD 编码、检验值…
│   └── （后续扩展）
└── {pack}/data_elements.json      ← 【案件层】仅追加，可缺省
```

| 层 | 内容 | 谁维护 |
|---|---|---|
| **全域基础** | 身份证、手机、日期、金额、姓名、性别、证件类型 | 平台（标准） |
| **行业叠加** | 金融：SWIFT/账号/卡号；医疗：病历号/ICD/检验值 | 行业专家 |
| **案件追加** | 该案特有（如特定案件类别代码） | 案件配置 |

**加载顺序**：全域基础 → 行业叠加 → 案件追加（后者覆盖前者，须显式声明）。

### 3.0.4 现成模式：照搬代码表合并

`load_code_tables`（REQ-D-003 AC-4）已实现所需结构：

```python
for v in vals:
    if v not in bucket:
        bucket.append(v)   # 仅追加，不改写/删除标准值
```

**数据元全域化完全照搬此模式**，设计原则无需重新论证。

### 3.0.5 🔴 关键风险：遮蔽 fail-closed 连锁

现有校验（`ontology_loader.py:821`）：

```python
if spec.get("sensitive") and (name, p) not in mask_set:
    raise ValueError(...)   # 引用敏感数据元但包级 policies 未声明遮蔽 → 硬失败
```

**连锁后果**：全域化后，任何引用全域敏感数据元（如 `DE_IDCARD`）的包，
必须在**自己的** policies.json 声明遮蔽，否则**装载直接失败**。

> ⚠️ 这推翻了"直接给 default 挂 DE_IDCARD"的做法——会装载失败。

**这个 fail-closed 本身正确**（敏感字段必须有遮蔽才能用），**缺的是"默认遮蔽"这一环**。

**处理方案**：全域声明 `mask` 作为默认值，包级可覆盖，二者皆无仍硬失败。

```jsonc
// _shared/data_elements.json
"DE_IDCARD": {
  "name": "公民身份号码", "type": "string", "sensitive": true,
  "mask": "idcard",        // ★ 默认遮蔽（沿用现有"前6后4"）
  "clean_rule": ["strip_cc", "digits_only"],   // ★ 见 §3.2 阶段二
  "format": "^[1-9]\\d{5}(19|20)\\d{2}..."
}
```

### 3.0.6 行业维度绑定：金融 / 医疗

现状：全库**无 industry 概念**。建议 `pack_meta.json`（可选，向后兼容）：

```jsonc
// ontology/{pack}/pack_meta.json
{ "industry": "金融" }
```

| 情况 | 行为 |
|---|---|
| 文件存在且 industry 合法 | 加载 `_industry/{行业}/` |
| 文件不存在 | **仅加载全域层**（向后兼容，现有包不受影响） |
| industry 值不在允许集 | **硬失败**（fail-closed） |

### 3.0.7 覆盖 vs 仅追加

| 策略 | 说明 | 评价 |
|---|---|---|
| 严格仅追加 | 照搬代码表，标准值不可覆盖 | 最安全，灵活性差 |
| **允许覆盖但显式声明**（建议） | 案件可放宽 format（兼容历史身份证），须 `override: true` + 落审计 | 平衡 |

### 3.0.8 全域化实施清单

| 优先级 | 事项 | 成本 |
|---|---|---|
| **P0-1** | 建 `_shared/data_elements.json`（迁移通用数据元） | 0.3 人日 |
| **P0-2** | `_shared` 加默认 `mask` 字段（**解决遮蔽连锁**） | 0.3 人日 |
| **P0-3** | 改 `load_data_elements` 支持三层合并 | 0.5 人日 |
| P1-4 | 建 `_industry/金融`、`医疗`（可先空壳） | 0.3 人日 |
| P1-5 | `pack_meta.json` industry 绑定 + 白名单校验 | 0.2 人日 |
| P1-6 | 两包迁移（default/reqd_case 去重，改引用全域） | 0.3 人日 |
| P2-7 | 覆盖审计留痕 | 0.2 人日 |

> **P0-2 不可跳过**：不做则引用全域敏感数据元的所有包装载失败。
> **P0-3 是 §3.1 起所有环节的前置**：不改加载器，接入态质检读不到全域 `format`。

---

## 3. 具体实施方案

### 3.1 阶段一：快赢（约 1 人日）

| # | 事项 | 文件 | 成本 |
|---|---|---|---|
| 1-1 | **列画像接入向导** | `IngestView.vue` 映射表加三列 | 0.3 人日 |
| 1-2 | 导入后自动触发语义态质检 | `worker/import.py` 末尾 enqueue | 0.3 人日 |
| 1-3 | ETL 页标注"需重跑 BUILD 生效" | `etl.py` 相关视图 | 0.1 人日 |
| **1-4** | **数据元推荐前移到 Step1**（v1.1） | `IngestView.vue` 渲染 `element_hints` + 采纳 | 0.3 人日 |
| **1-5** | **删除 Step4 异步推荐任务**（v1.1） | 删 `worker/recommend.py`，**省 0.5 人日** | -0.5 人日 |

**1-4 具体实现**（`element_hints` 已由 analyze 同步返回，纯前端）：

```vue
<!-- 映射表增加"数据元"列 -->
<td>
  <span v-if="elementHintOf(col.name)">
    {{ elementHintOf(col.name).element_name }}
    （{{ Math.round(elementHintOf(col.name).confidence * 100) }}%）
    <NButton size="tiny" @click="adoptElement(col.name)">采纳</NButton>
  </span>
  <span v-else class="dim">—</span>
</td>
```

采纳 → 写入 `bindings.data_element`（接 P0-2 回写机制，见 §3.6）。

**1-1 具体实现**（数据已现成，纯前端）：

```vue
<!-- 映射表增加三列 -->
<td class="dim">{{ sampleOf(col.name) }}</td>       <!-- samples 前 2 个 -->
<td>{{ nullRateOf(col.name) }}%</td>                 <!-- null_rate -->
<td>{{ inferTypeOf(col.name) }}</td>                 <!-- inferred_type -->
```

### 3.2 阶段二：地基（约 2–3 人日）

| # | 事项 | 说明 | 成本 |
|---|---|---|---|
| 2-1 | **数据元补 `clean_rule`** | 声明"该怎么洗"，否则"不符合数据元要求"无判据 | 0.5 人日 |
| 2-2 | **补 split op（复合列拆分）** | 🔴 最大缺口，见 §3.4 | 1–2 人日 |
| 2-3 | 扩充 op 集 | 见 §3.3 建议新增 | 0.5–1 人日 |

**2-1 schema**：

```jsonc
// data_elements.json
"DE_PHONE": {
  "name": "手机号", "type": "string", "format": "^1[3-9]\\d{9}$",
  "clean_rule": ["strip_cc", "digits_only"],   // ★ 新增
  "mask": "phone"                               // 见全域化方案
}
```

装载期沿用 REQ-D-001 校验：op 必须已在 `clean_ops` 注册（fail-closed）。

### 3.3 现有 op 能力矩阵（11 个）

| op | impl | layer | 用途 |
|---|---|---|---|
| `despace` | py | any | 去空格 |
| `digits_only` | py | any | 仅留数字 |
| `strip_cc` | py | any | 去国家码 |
| `strip_paren` | py | any | 去括号内容 |
| `to_upper` / `to_lower` | py | any | 大小写 |
| `pad_date` | py | any | 日期补零 |
| `reject_if` | py | clean | 条件剔除（**会丢行**） |
| `cn_date_norm` | sql | transform | 中文日期归一 |
| `strip_thousands` | sql | transform | 去千分位 |
| `strip_currency` | sql | transform | 去币种符 |

**建议新增**（按优先级）：

| op | 用途 | 类别 |
|---|---|---|
| **`split`** | 复合列拆分 | 🔴 最高（当前完全无能力） |
| `trim_prefix` / `trim_suffix` | 去固定前后缀 | A |
| `regex_extract` | 正则提取 | B（可能丢信息） |
| `unit_convert` | 量纲换算（元/万元） | B |

### 3.4 🔴 split op 设计（最关键）

现状：`clean_ops.split_op` 只是**解析 token 字符串**（`"reject_if:contains_mask"` → `("reject_if","contains_mask")`），
**不是拆分数据的操作**。

而复合列是接入最常见的问题：

- `de_recommend` 有 `split_hint`（混装落点，提示上游拆分）
- ETL 有 `A_split_source_sql` 出路
- **两者都没有实现**

**建议设计**：

```jsonc
"clean": ["split:sep=、:into=name,idcard"]
```

| 参数 | 说明 |
|---|---|
| `sep` | 分隔符（支持正则） |
| `into` | 拆分后的目标属性列表 |
| `on_unequal` | 段数不匹配时：`reject_row`（默认，隔离）/ `pad_null` / `keep_first` |

**产出**：拆出的段落映射到 `into` 指定属性；`on_unequal` 的行进 `build_quarantine` 隔离区（已有机制）。

### 3.5 阶段三：接入态质检（约 2–3 人日）

新建 `core/ingest_quality.py`，对**原始暂存件 DataFrame** 扫描：

| 扫描项 | 接入态可做 | 说明 |
|---|---|---|
| 空值率 | ✅ | 已有 `null_rate`，超阈值告警 |
| 类型推断冲突 | ✅ | `inferred_type` vs 声明类型 |
| 复合列嫌疑 | ✅ | `value_type.analyze_column` 的 `mixed` |
| **数据元合规（format/enum）** | ✅ | 待 2-1 完成才有判据 |
| 敏感信息嫌疑 | ⚠️ | **待确认**（见 §6） |
| 量纲异常 | ⚠️ | 需数据元 `unit` |

**接口**：

```
POST /cases/{cid}/sources/{uid}/ingest-quality
→ { issues: [{ col, type, severity, message, fix: {class:"A"|"B"|"C", op?, suggestion?} }] }
```

### 3.6 阶段四：处置界面（约 2–3 人日）

| # | 事项 | 成本 |
|---|---|---|
| 4-1 | Step2 页面：问题清单 + 三分类 | 1.5 人日 |
| 4-2 | B 类预演（`/etl-preview` 返回影响行数） | 1 人日 |
| 4-3 | 处置 → config 草稿 → 复核发布 | 0.5 人日（复用草稿区） |

### 3.7 v1.1：数据元前移带来的闭环（关键收益）

```
Step1 推荐「手机号 → DE_PHONE」→ 采纳 → 写 bindings.data_element
        ↓
Step2 用 DE_PHONE.format 校验 / 用 DE_PHONE.clean_rule 选 op
        ↓
Step3 导入 → Step5 BUILD
        ↓
compliance / unit_scan 因 prop_data_elements 非空而真正执行
```

**此前讨论的四个问题同时有解**：

| 此前问题 | 前移后 |
|---|---|
| 数据元在接入中没作用 | ✅ 采纳即写 `bindings.data_element` |
| 采纳后无处回写 | ✅ 回写点明确（bindings，非草稿） |
| 质检没有判据 | ✅ Step2 用已采纳数据元的 format/enum |
| ETL 不知道怎么洗 | ✅ 从数据元 `clean_rule` 派生 op |

---

## 4. 双阶段质检架构

```
接入态质检（扫原始件 DataFrame）    语义态质检（扫 obj_* 物化表）
        ↓                                    ↓
   拦脏数据进语义层                    验语义层质量
   Step2 处置                          BUILD 后自动
        ↓                                    ↓
   → BUILD →                          报告 + 处置回路
```

| 维度 | 接入态 | 语义态 |
|---|---|---|
| 输入 | 暂存件 DataFrame | `OntologyReadGateway` |
| 时机 | Step1 后自动 | BUILD 后自动 |
| 关注 | 脏数据、格式、复合列 | 合规/新鲜度/敏感/量纲 |
| 处置 | **界面 ETL 三分类** | 报告 + 跳 ETL 设置 |
| 状态 | 新增 | 已有（保留） |

**两者都留**——正是用户说的"后续过程中也可以对于数据进行验证"。

---

## 5. 实施路线

| 阶段 | 内容 | 成本 | 建议 |
|---|---|---|---|
| **〇、全域化前置**（v1.2） | `_shared` + 默认 mask + 三层加载器 + 行业目录 | **1.1 人日** | ✅ **最先做**（下游全依赖） |
| **一、快赢** | 列画像展示 + 自动质检 + 标注 + **元推荐前移 Step1** − 删除异步任务 | **1 人日**（v1.1 净减 0.5） | 紧随阶段〇 |
| **二、地基** | clean_rule + split op + 扩 op | 2–3 人日 | 做完界面才有意义 |
| **三、前移** | 接入态质检新入口 | 2–3 人日 | |
| **四、界面** | 三分类处置 + 预演 | 2–3 人日 | |

**合计约 8–11 人日（v1.1 去重 −0.5；v1.2 合入全域化 +1.1）。**

> **顺序不可颠倒，两个依赖必须先看**：
> 1. **阶段〇是下游全部前置**——不改三层加载器，接入态质检读不到全域 `format`，
>    ETL 处置读不到 `clean_rule`，遮蔽拿不到默认 `mask`。
> 2. **不做阶段二就做阶段四**，界面会大量出现"仅建议"——
>    用户看到一堆问题却什么都做不了，体验反而更差。
>
> **最小可用组合**：阶段〇（1.1）+ 阶段一（1.0）= **约 2 人日**，
> 即可让数据元全域共享、列画像可见、推荐前移且采纳有回写。

---

## 6. 待确认事项

| # | 事项 | 说明 |
|---|---|---|
| 1 | **接入态质检是否扫敏感信息？** | 接入态扫原始件会发现未映射列的敏感数据。有价值，但涉及"原始数据是否入诊断"的边界 |
| 2 | 复合列 `on_unequal` 默认策略 | 建议 `reject_row`（进隔离区），但可能丢较多行 |
| 3 | B 类预演的样本量 | 全量还是前 N 行？影响性能与准确性 |
| 4 | 处置是否需审批 | 当前 `require_analyst`（clearance≥2）够不够 |
| 5 | **`/c/suggest` 独立页是否下线？**（v1.1） | 前移后其内容在向导内即可完成，建议降级为入口或下线 |
| 6 | **Step1 采纳是写 bindings 还是草稿？**（v1.1） | 建议写 bindings（映射期决策属配置），但需确认是否沿用"草稿+发布"红线 |

---

## 7. 验收标准

| 编号 | 用例 | 预期 |
|---|---|---|
| IN-TC-01 | 向导映射表 | **显示样本值 / 空值率 / 推断类型**（当前无） |
| IN-TC-02 | 导入完成 | **自动触发**语义态质检（当前需手动） |
| IN-TC-03 | ETL 页 | 标注"需重跑 BUILD 生效" |
| IN-TC-04 | 复合列接入 | Step2 显示 **C 类建议**（当前无提示） |
| IN-TC-05 | 金额列含千分位 | 显示 **A 类「应用 strip_thousands」** 并可一键应用 |
| IN-TC-06 | `reject_if` 处置 | **先预演显示影响行数**，确认后才写入 |
| IN-TC-07 | 处置后 | 生成 config 草稿，**不自动生效** |
| IN-TC-08 | 数据元 format 不匹配 | 提示"不符合数据元要求"并给处置建议 |
| IN-TC-09 | 拆分后段数不匹配 | 按 `on_unequal` 处理，异常行进隔离区 |
| IN-TC-10 | 接入态 + 语义态 | **双阶段都产出报告**，互不替代 |
| **IN-TC-11** | **Step1 映射表**（v1.1） | **直接显示数据元推荐 + 采纳按钮**（当前 Step1 无，Step4 才有） |
| **IN-TC-12** | **Step1 采纳后**（v1.1） | `bindings.data_element` **被写入**（当前采纳后无回写） |
| **IN-TC-13** | **删除异步推荐任务后**（v1.1） | 推荐结果**与删除前一致**（无行为回归） |
| **IN-TC-14** | **Step2 质检**（v1.1） | 能给出"不符合数据元 X 的 format"提示（当前无判据） |
| **IN-TC-15** | **BUILD 后**（v1.1） | 采纳过数据元的对象，`compliance`/`unit_scan` **真正执行**（当前全部跳过） |
| **DE-TC-01** | **default 包装载**（v1.2） | `prop_data_elements` 引用总数 **> 0**（当前 0） |
| **DE-TC-02** | **新案件（default 包）**（v1.2） | `unit_scan`/`compliance` **不再全部跳过** |
| **DE-TC-03** | **包隔离**（v1.2） | 在 reqd_case 补数据元，对 default 案件**无影响**（符合预期） |
| **DE-TC-04** | **全域敏感数据元**（v1.2） | default 引用 `DE_IDCARD` **装载成功**（默认 mask 生效） |
| **DE-TC-05** | **包级遮蔽覆盖**（v1.2） | 包级 policies 声明**优先于**全域默认值 |
| **DE-TC-06** | **红线不放松**（v1.2） | 全域敏感数据元 + 包无遮蔽 → **仍硬失败** |
| **DE-TC-07** | **无标准漂移**（v1.2） | 两包引用同一全域数据元，`format` **一致** |
| **DE-TC-08** | **行业绑定**（v1.2） | `pack_meta` 声明 `industry: 金融` → 加载到金融数据元 |
| **DE-TC-09** | **向后兼容**（v1.2） | `pack_meta` 缺失 → **仅加载全域层**，不报错 |
| **DE-TC-10** | **非法行业值**（v1.2） | **硬失败**（fail-closed） |
| **DE-TC-11** | **覆盖审计**（v1.2） | 案件覆盖全域 format 需 `override: true`，否则拒绝 |

---

## 8. 一句话总结

> **质检前移（双阶段）、ETL 界面执行（A/B/C 三分类）、画像消费（列画像已算未展示）、
> 数据元推荐前移（从队尾到队首）——四者是同一条链上的缺口，必须一起改。**
>
> **v1.1 最关键的判断**：数据元推荐**不只是晚，是位置错了**——
> 它是 Step2 质检与处置的**判据输入**（format/enum/clean_rule），却排在 Step4；
> 且 `analyze` 已同步算好被丢弃，`worker/recommend` 又重算一遍（**算了两遍**）。
> 前移后，此前"质检游离 / ETL 没作用 / 数据元没作用 / 采纳无回写"四个问题**同时有解**。
>
> **v1.2 最关键的判断**：数据元「**从哪来**」（§3.0 全域化）与「**在哪用**」（§3.1 起接入消费）
> 是同一条链的两端，分开改必然打架——接入态质检要读的 `format`、ETL 要读的 `clean_rule`、
> 遮蔽要读的 `sensitive`/`mask`，全由全域化的三层结构定义。
> **故合为一份，且阶段〇（全域化）必须最先做**。
>
> **最快见效的是列画像展示（0.3 人日）**：后端已算好 `inferred_type`/`null_rate`/`samples`，
> 前端零消费——纯接线，当天可验证。
>
> **最大前置缺口是 split op**：`split_op` 只是解析 token 的函数，不是拆分操作；
> 而复合列是接入最常见的问题，且 `de_recommend.split_hint` 与 ETL `A_split_source_sql` 都指向它却都没实现。
>
> **所有处置都不自动生效**：生成 config 草稿 → 复核 → 发布 → 重跑 BUILD（与"采纳建议进草稿区"同一条红线）。
