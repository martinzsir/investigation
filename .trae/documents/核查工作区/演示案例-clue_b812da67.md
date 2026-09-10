# 业务演示案例：clue_b812da67「张卫国 · 中标-资金时间窗碰撞」

| 项 | 内容 |
|---|---|
| 日期 | 2026-09-10 |
| 依据 | [实施方案.md](./实施方案.md)（待评审） |
| 案件 | demo_1 · v7 产物 |
| 线索 | `clue_b812da67` · skill=xu_shi · 规则 R6 · priority_score 0.595 |
| 用途 | 实施后的业务动线演示脚本，各幕可对照测试组单独验收 |

---

## 0. 素材事实（真实产物）

来源：`cases/demo_1/artifacts/clues_v7.json` L72-149。

- **命中规则 R6**：招投标中标公示日前后 20 天内，与中标项目相关的 1 万元整数倍资金交易，且资金主体为个人（名称不含"公司"等单位后缀）。
- **线索内容**：张卫国（个人）在 7 个市政工程中标公示日后 **5~18 天**内各有 **10 万元整数倍**个人账户资金进出。
- **source_rows 7 行**：城东管网(偏移5天)、滨江路改造(7)、安置房二期(7)、市政绿化(8)、桥梁加固(10)、智慧交通(13)、安置房一期(18)，公示日跨度 2019-06-18 ~ 2023-08-15。
- **assumption_chain**: H4（财物通过第三方过桥）；evidence_builder 假设关键词命中 H1（收受财物·异常整数现金存入）。
- **状态**：待查 · needs_human_review=true · 定性_policy=AI 不给出定性，须言词证据+法定程序。
- **两个自动核查项稳定键**（ADR-V-4 算法 `vi_ + sha1(clue_id|kind|text)[:16]`，已实测）：

| item_id | kind | text |
|---|---|---|
| `vi_c60df5a73b778c2c` | inference | 中标公示 ±20 天邻接边上出现整数资金、且资金主体为个人（非对公单位） |
| `vi_ffb0591ea0dd303f` | pending_hypothesis | 待验证假设：H1（收受财物（异常整数现金存入）） |

---

## 第一幕：打开线索详情——核查清单自动供给（P1 · REQ-V-002/006/007）

侦查员王峰打开 `GET /cases/demo_1/clues/clue_b812da67`：

**三栏证据**（只读纪律不变）：
- 事实栏 7 张卡片：`项目: 城东管网 · 资金主体: 张卫国 · 金额: 100000 · 偏移天数: 5 · 中标公示日: 2020-03-25`（其余 6 行同理）
- 推断栏 1 张：`中标公示 ±20 天邻接边上出现整数资金、且资金主体为个人（非对公单位）`
- 待核实栏 2 张：`待验证假设：H1（收受财物（异常整数现金存入））`、`规则判据：招投标中标公示日前后 20 天内…`

**证据下方新增「核查工作区」**，首次打开自动落 2 个核查项（上表），`origin='auto'`，`progress: {total:2, concluded:0, pending:2}`；三栏待核实卡片右上角出现「待核查」徽标，点击滚动定位到工作台对应行。

> 规则判据（r 卡）按方案 §9 收敛**不生成核查项**——那是规则原文留痕，不是可裁决任务。

**验收对照**：供给幂等（刷新不重复建项、已有结论不覆盖）→ test_verifyitem。

## 第二幕：人工拆解核查任务（P1 · REQ-V-005/006）

王峰把 H1 假设拆成可操作动作，在工作台手动添加：

```http
POST /cases/demo_1/clues/clue_b812da67/verify-items
{"text": "调取张卫国账户在 7 个公示日 ±20 天的完整流水，确认资金来源与去向"}
```
返回 `202 + task_id`（TASK_VERIFY op=add_manual，idem_key=verify-add:{clue_id}:{sha1(text)}，重复提交不重复建项）。

```http
POST .../verify-items
{"text": "确认张卫国与 7 个中标项目的承包关系（是否分包方/材料供应商）"}
```

工作台变为 4 项，`origin=manual` 的两行带「人工」徽标，progress `{total:4, concluded:0, pending:4}`。

**验收对照**：幂等键去重、跨租户 404、未登录 401 → test_verifyapi。

## 第三幕：逐项核查——结论、书证、AI 复跑（P1+P2+P3）

### 3.1 流水核查（manual 项 · P1+P2）

开始核查 → 核查中。调银行回执后上传书证（REQ-V-009/010）：

```http
POST /cases/demo_1/clues/clue_b812da67/evidence        # multipart
→ 200 {"material_id":"ev_a1b2c3d4e5f6","sha256":"9f86d0…","size":184320}
```

挂接到该项（REQ-V-011）后，核查项展开区出现 `materials:[{orig_name:"招行流水回执.pdf", material_type:"付款凭证"}]`。随后裁决：

```http
POST .../verify-items/{item_id}/transitions
{"next_status":"已证实","conclusion":"7 笔 10 万均于公示日后 5-18 天到账，付款方为城东建材经营部（张小满，个人独资），系 7 个项目砂石供应商"}
```

Worker 校验通过，`audit_chain` 追加 `verify_item_transition`（含 before/after/operator=王峰）。

**演示点**：不填结论提交"已证实" → 任务 FAILED `CONCLUSION_REQUIRED`（REQ-V-003）。

### 3.2 AI 复跑（P3 · REQ-V-017）

王峰点「运行内核核查」→ 只读 Function 时间窗复跑，结果回填琥珀边框卡片：`AI辅助推演·需人确认：7/7 行时间邻接关系复核成立，溯源 source_row_ids=[…]`。**status/conclusion 不动**——定性仍由人做。

### 3.3 H1 假设项（P1）

`待验证假设：H1` → 核查中 → **已证实**（结论引用 3.1 流水证据）。

### 3.4 Agent 提案（P2 · REQ-V-014）

Agent 经 `submit_proposal(kind=verify_item)` 建议"比对张卫国与张小满通话频次"→ 王峰审批通过 → 自动生成 TASK_VERIFY 任务且 **operator=王峰**（agent 名只留在 proposed_by）。驳回则无任务产生。

**验收对照**：test_verifyitem（Worker 三态错误）+ test_evidence_file + test_verify_replay + 提案桥接用例。

## 第四幕：库内查不动——调取台账（P2 · REQ-V-012/013）

「承包关系」项库内核实不了 → 王峰建调取清单：

| 字段 | 值 |
|---|---|
| target | 市住建局 |
| material | 7 个项目中标评分表及分包合同 |
| legal_instrument | 调函〔2026〕12 号 |
| due_date | 2026-09-20 |
| status | 待发起 → 已发起 |

核查项标 **无法核实**（外部调取中，**不算未结、不阻塞固证**）。9 月 21 日起台账列表该行自动显示红色「超期」徽标（读面派生，存储 status 不变）。

**验收对照**：test_verifyreq（状态机 + overdue 派生 + 幂等留审计）。

## 第五幕：固证门禁——闭环的强制点（P1 · REQ-V-008/015）

此时 inference 项 `vi_c60df5a73b778c2c` 还没裁决，王峰直接点「固证」：

```json
// TASK_DISPOSE confirm → Worker 拒绝
TaskExecError: VERIFY_PENDING
"尚有 1 项核查未结：中标公示 ±20 天邻接边上出现整数资金…（先逐项得出结论，或标记无法核实）"
```

线索状态仍「查证中」，审计链**无**迁移事件。前端确认弹窗顶部同步显示琥珀警示"尚有 1 项核查未结（服务端将拒绝）"。

王峰裁决该推断项：**已证实**（7 行 source_rows 逐行复核无误，非误报）。progress `{total:4, concluded:4, pending:0}` → 再点固证 → 成功。审计链追加：

```json
{"event":"confirm_summary","clue_id":"clue_b812da67",
 "items":[{"text":"中标公示 ±20 天…","status":"已证实","conclusion":"7 行复核无误"},
          {"text":"待验证假设：H1…","status":"已证实","conclusion":"流水佐证"},
          {"text":"调取张卫国账户…","status":"已证实","conclusion":"付款方为城东建材经营部"},
          {"text":"确认张卫国与 7 个项目…","status":"无法核实","conclusion":"住建局调取中"}],
 "operator":"王峰","ontology_version":"v7"}
```

处置结论不再是"一段自由文本"，而是**结构化快照进哈希链**。

**验收对照**：verifyapi 门禁用例（pending>0 拦截 / total=0 回归兼容 / exclude 同拦 / file·verify·reset 不受影响）。

## 第六幕：红线与翻案（全程可演示）

- **Agent 红线**：以 `operator=agent:sunzi` 提交任何转移 → 任务 FAILED `VERIFY_FORBIDDEN`（对齐 clue_transition 红线）；
- **翻案留痕**：后续张小满账户被查实系张卫国岳母代持 → 「已证实」的流水项点**重开** → 核查中，补结论后再证实；旧结论在审计链 before/after 里完整可溯；
- **链完整性**：`chain_verify()` 对整个 audit_chain 校验通过。

**验收对照**：test_verifyitem 红线用例 + 既有 m3chain/statestore 回归。

---

## 幕次 ↔ 需求 ↔ 测试组对照表

| 幕 | 需求 | 分期 | 测试组 |
|---|---|---|---|
| 一 | REQ-V-002/006/007 | P1 | verifyitem / verifyapi / 前端 spec |
| 二 | REQ-V-005/006 | P1 | verifyapi |
| 三 | REQ-V-009/010/011/014/017 + REQ-V-003 | P1+P2+P3 | verifyitem / evidencefile / verifyreplay |
| 四 | REQ-V-012/013 | P2 | verifyreq |
| 五 | REQ-V-008/015 | P1 | verifyapi（门禁） |
| 六 | 红线回归 | P1 | verifyitem / m3chain / statestore |

## 演示收益

固证时审计链里躺着的是"谁在何时对哪条疑点依据哪份书证得出什么结论"的完整证据包，而不是一句"情况属实，予以固证"。
