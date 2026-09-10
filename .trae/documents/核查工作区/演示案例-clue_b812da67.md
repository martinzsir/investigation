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

- **核查手册建议项**（REQ-V-018，第二幕主用）：`ontology/default/verify_playbooks.json` 的 `pb_r6_bid_fund_window` 按 rule_id=R6 / assumption=H1 匹配本线索，槽位从 7 行 source_rows 确定性填充（{subject}=张卫国、{project_count}=7），渲染 3 条 `origin='suggested'`、`status='建议'` 的建议卡：
  1. 「调取张卫国账户在 7 个中标公示日 ±20 天的完整流水…」——渠道 **function**（`time_window_collision`，带证伪条件）；
  2. 「核实张卫国与 7 个中标项目的承包/分包/材料供应关系」——渠道 **external**（target=项目发包/主管单位，fallback `org_interest_links`）；
  3. 「核查整数资金付款方与张卫国的关联（亲属/代持/过桥）」——渠道 **function**（`overpass_two_hop`）。
  建议项**不进固证门禁 pending**；采纳/改/忽略都是人的写动作，全程纯离线、无 LLM。

- **LLM 部署面前置**（REQ-V-019 + ADR-V-8，第三幕 3.5 主用）：红线按部署面分三档——**off**（default 包默认，isolated 拒一切模型调用）/ **local**（本机 Ollama，数据不出机，relaxed 脱敏）/ **cloud**（公网 API，strict 脱敏 + HTTPS 白名单 + clearance 授权）；会话网络面与策略面取交集、fail-closed。3.5 按 off → local → cloud 三档递进演示；任何档位 L1 手册建议均不受影响。

---

## 第一幕：打开线索详情——核查清单自动供给（P1 · REQ-V-002/006/007）

侦查员王峰打开 `GET /cases/demo_1/clues/clue_b812da67`：

**三栏证据**（只读纪律不变）：
- 事实栏 7 张卡片：`项目: 城东管网 · 资金主体: 张卫国 · 金额: 100000 · 偏移天数: 5 · 中标公示日: 2020-03-25`（其余 6 行同理）
- 推断栏 1 张：`中标公示 ±20 天邻接边上出现整数资金、且资金主体为个人（非对公单位）`
- 待核实栏 2 张：`待验证假设：H1（收受财物（异常整数现金存入））`、`规则判据：招投标中标公示日前后 20 天内…`

**证据下方新增「核查工作区」**，首次打开自动落 2 个核查项（上表，`origin='auto'`）+ 3 张虚线建议卡（REQ-V-018，第二幕展开），`progress: {total:2, concluded:0, pending:2, suggested:3}`——建议项独立计数、**不进 pending**；三栏待核实卡片右上角出现「待核查」徽标，点击滚动定位到工作台对应行。

> 规则判据（r 卡）按方案 §9 收敛**不生成核查项**——那是规则原文留痕，不是可裁决任务。

**验收对照**：供给幂等（刷新不重复建项、已有结论不覆盖）→ test_verifyitem。

## 第二幕：建议项审查——拆案从"手写"变"采纳"（P1 · REQ-V-018/005/006）

王峰不需要自己拆案。核查工作区里，2 个自动核查项下方列着 3 张**虚线建议卡**（核查手册 `pb_r6_bid_fund_window` 渲染，实体槽位已从 7 行 source_rows 填好）：

| 建议核查项（已填好，可改） | 维度 | 渠道徽标 |
|---|---|---|
| 调取**张卫国**账户在 **7** 个中标公示日 ±20 天的完整流水，核实资金来源与去向 | 资金 | 「库内可复跑」→ time_window_collision |
| 核实**张卫国**与 **7** 个中标项目的承包/分包/材料供应关系 | 关系 | 「需外部调取」（ fallback：库内先试 org_interest_links ） |
| 核查整数资金付款方与**张卫国**的关联（亲属/代持/过桥） | 关系 | 「库内可复跑」→ overpass_two_hop |

王峰逐条审查：

1. **流水项**点「采纳」：

```http
POST /cases/demo_1/clues/clue_b812da67/verify-items/{item_id}/transitions
{"next_status": "待核查"}
```
返回 `202 + task_id`（TASK_VERIFY op=transition，建议→待核查；idem_key=verify:{item_id}:待核查:）。审计链 before=`{status:"建议", text:"调取张卫国账户…"}`、after=`{status:"待核查"}`，operator=王峰。

2. **承包关系项**点「改一改」——文本末尾补"（重点查砂石供应合同）"→ 确认采纳：

```http
POST .../verify-items/{item_id}/transitions
{"next_status": "待核查", "text": "核实张卫国与 7 个中标项目的承包/分包/材料供应关系（重点查砂石供应合同）"}
```
建议原文留在审计 before，改写文本落 after（可溯：系统建议了什么、人改了什么）。

3. **付款方关联项**点「采纳」。

工作台变为 **5 个正式核查项**（2 auto + 3 suggested 已采纳），`progress: {total:5, concluded:0, pending:5, suggested:0}`；两张「库内可复跑」项展开区出现「运行内核核查」按钮（第三幕 3.2），「需外部调取」项出现「转调取台账」按钮（第四幕，target/material 已预填）。

> **手册外动作**走结构化构造器（不再是空白文本框）：选对象（事实卡实体下拉，如张小满）→ 选维度（通讯）→ 选渠道（库内复跑）→ text 自动拼装、可再编辑 → 提交仍走 op=add_manual（idem_key=verify-add:{clue_id}:{sha1(text)}，重复提交不重复建项）。本例通讯/轨迹维度手册未覆盖，由第三幕 3.4（Agent 提案）与 3.5（LLM 草案）补充。
>
> **LLM 未参与本幕**：建议全部来自声明式 playbook 纯离线渲染；王峰也可以不理睬任何建议直接固证——建议项不进门禁。

**验收对照**：建议渲染（槽位/渠道字段）、采纳/改/忽略转移、幂等不覆盖、建议不计数门禁 → test_verifysuggest；API 幂等键/跨租户 404/未登录 401 → test_verifyapi；建议卡与构造器交互 → VerifyWorkbench.spec.ts。

## 第三幕：逐项核查——结论、书证、AI 复跑（P1+P2+P3）

### 3.1 流水核查（建议采纳项 · P1+P2）

第二幕采纳的流水项「开始核查」→ 核查中。调银行回执后上传书证（REQ-V-009/010）：

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

两个「库内可复跑」建议项采纳后自带按钮，function 名直接取自手册（无需关键词映射）：

- 流水项点「运行内核核查」→ 只读 `time_window_collision` 复跑，琥珀边框卡片回填：`AI辅助推演·需人确认：7/7 行时间邻接关系复核成立，溯源 source_row_ids=[…]`；
- 付款方关联项点「运行内核核查」→ `overpass_two_hop` 复跑回填：`资金经城东建材经营部两跳与张卫国配偶账户相交（170 万），Cypher/SQL 双轨一致`。

**status/conclusion 不动**——复跑只填 replay_json。王峰看过复跑卡片后，自行把付款方关联项裁决为**已证实**（结论引用两跳路径+工商互证；这是人点的，不是系统判的）。

### 3.3 H1 假设项（P1）

`待验证假设：H1` → 核查中 → **已证实**（结论引用 3.1 流水证据）。

### 3.4 Agent 提案（P2 · REQ-V-014）

通讯维度是本线索 playbook 未覆盖的方向（手册 3 条均为资金/关系）——这正是提案补充层的位置：Agent 经 `submit_proposal(kind=verify_item)` 建议"比对张卫国与张小满通话频次"→ 王峰审批通过 → 自动生成 TASK_VERIFY 任务且 **operator=王峰**（agent 名只留在 proposed_by）。驳回则无任务产生。

**验收对照**：test_verifyitem（Worker 三态错误）+ test_evidence_file + test_verify_replay + 提案桥接用例。

### 3.5 AI 建议核查方向（P2 · REQ-V-019 + ADR-V-8，三档递进：off → local → cloud）

工作区底部「AI 建议核查方向」按钮，档位由会话网络面与 llm_policy 交集决定。本幕按三档递进演示。

#### 3.5.1 off 档——默认隔离（零模型调用）

default 包未声明 deployments 段（等价 `network=isolated`）：按钮**置灰**并提示"内核隔离模式，LLM 能力关闭"。王峰强制发起：

```http
POST /cases/demo_1/clues/clue_b812da67/verify-items/draft
→ 200 {"degraded":true, "mode":"off", "reason":"会话/策略交集无可用部署档（isolated）"}
```

- llm_call_log 落 allowed=false 一条，**零模型调用**；L1 三张手册建议卡与手动构造器照常工作。
- 旧策略文件（无 deployments 段）装载后 local/cloud 均 disabled——fail-closed 回归。

#### 3.5.2 local 档——本机 Ollama（数据不出机，演示主力）

案件包声明 `deployments.local.enabled=true`，`base_url=http://127.0.0.1:11434/v1/chat/completions`，`redaction=relaxed`；王峰会话 network=local。按钮变为可点，标签"**本地模型（数据不出机）**"——无出网警示。点击：

```http
POST /cases/demo_1/clues/clue_b812da67/verify-items/draft
→ 200 {"mode":"local","proposals":[
   {"proposal_id":"pp-7f31…","text":"比对张卫国与张小满在 7 个公示日前后的通话频次突增","dimension":"通讯","channel":"function","ref_function":"call_frequency_spike","author":"model:qwen2.5:7b","dedup":true},
   {"proposal_id":"pp-7f32…","text":"核查张卫国与张小满在 7 个公示日前后是否轨迹同框","dimension":"行为","channel":"function","ref_function":"co_located_pairs","author":"model:qwen2.5:7b"}],
   "degraded":false}
```

- **端点闸**：请求前 `assert_endpoint_allowed` 校验 base_url——loopback 放行；演示反向用例：把 base_url 改成 `http://llm.corp.example.com`（DNS 解析为公网 IP）→ **硬失败 + llm_call_log(blocked)，请求根本不发出**；解析失败同理拒绝（防"名义 local 实发公网"绕过）。
- **relaxed 脱敏**：发给本机模型的上下文含聚合事实（7 行碰撞/金额量级/时间窗）、R6 rule_text、H1/H3 证据需求与证伪条件、Function 目录、庙算维度缺口（G-024）；人名/单位**可明文**（张卫国/张小满，免服务端 rehydrate）——但精确轨迹点、通话正文**仍整段丢弃**（本地服务可能自有日志，此两类不豁免），无流水明细，prompt 不留存。
- **shadow 隔离**：调用前后 `shadow_diff` 为空——核查项表/语义层零变化，只动 proposal/llm_call_log/audit_chain。
- **去重**：通话候选与 3.4 Agent 提案内容哈希同源（3.4 已审批成项）→ 不重复提交、标 `dedup:true`；王峰审批轨迹同框提案通过 → 生成核查项 `origin='ai_draft'`、status='待核查'，**operator=王峰**、proposed_by=model:qwen2.5:7b，自带「运行内核核查」（ref_function=co_located_pairs）。

**AI 建议也可以被查否——结论永远是人裁的**：

- 轨迹项点「运行内核核查」→ `co_located_pairs` 复跑：7 个时间窗均无同框 → 王峰裁决**已查否**，结论引用 H3 证伪条件"通话/轨迹无异常则证伪"；
- 3.4 生成的通话项同理复跑 `call_frequency_spike`：频次无显著突增 → 王峰裁决**已查否**。

#### 3.5.3 cloud 档——公网 API（显式授权，strict 脱敏）

切换到 network=web 会话 + `deployments.cloud.enabled=true`（clearance 校验通过）。按钮标签变琥珀色"**公网模型（数据将出网）**"。点击后返回 `"mode":"cloud"`：

- **strict 脱敏**：出网 payload 人名以"人物1/人物2"tokenize、轨迹点/通话正文丢弃、只用聚合事实；模型产出的 token 文本由服务端 rehydrate 成真实实体后才入提案队列；
- **端点闸**：HTTPS only 且 host 必须在白名单（dashscope.aliyuncs.com）——配置成 `http://…` 明文或非白名单 host 硬失败、不触网；API key 仅从环境变量读取，不入库入日志；
- 同方向候选与 local 档草案内容哈希同源 → 幂等不重复提交；llm_call_log 重审计（operator=王峰、purpose、出网事实）。

**验收对照**：test_verifydraft（三档交集降级零调用 / 端点闸 loopback·公网IP·DNS失败·HTTPS白名单 / shadow_diff 双档隔离 / guard 丢弃非法 function 候选 / 审批桥接 operator=人 / relaxed·strict 双档脱敏断言 / 内容哈希幂等）+ llmpolicy 组（旧策略文件 fail-closed）。

## 第四幕：库内查不动——调取台账（P2 · REQ-V-012/013）

承包关系项（external 渠道）王峰先点「库内先试一把」（fallback `org_interest_links`）——无分包/供应登记命中 → 点该项自带的「转调取台账」，弹窗已按手册 external 预填 target/material，王峰补法律手续与期限后发起：

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

王峰裁决该推断项：**已证实**（7 行 source_rows 逐行复核无误，非误报）。progress `{total:7, concluded:7, pending:0, suggested:0}`（2 auto + 3 手册采纳 + 1 Agent 提案 + 1 AI 草案）→ 再点固证 → 成功。审计链追加：

```json
{"event":"confirm_summary","clue_id":"clue_b812da67",
 "items":[{"text":"中标公示 ±20 天…","status":"已证实","conclusion":"7 行复核无误"},
          {"text":"待验证假设：H1…","status":"已证实","conclusion":"流水佐证"},
          {"text":"调取张卫国账户…（手册建议采纳）","status":"已证实","conclusion":"7 笔 10 万均于公示日后 5-18 天到账，付款方为城东建材经营部（张小满，个人独资），系 7 个项目砂石供应商"},
          {"text":"核实张卫国与 7 个项目承包关系…（手册建议采纳+改写）","status":"无法核实","conclusion":"住建局调取中"},
          {"text":"核查整数资金付款方与张卫国关联…（手册建议采纳）","status":"已证实","conclusion":"两跳过桥路径与张卫国配偶账户相交（170 万），overpass 复跑+工商互证"},
          {"text":"比对张卫国与张小满通话频次…（Agent 提案审批）","status":"已查否","conclusion":"call_frequency_spike 复跑频次无显著突增，H3 证伪条件命中"},
          {"text":"核查二人公示日前后轨迹同框…（AI 草案审批）","status":"已查否","conclusion":"co_located_pairs 复跑 7 个时间窗均无同框，H3 证伪条件命中"}],
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
| 一 | REQ-V-002/006/007（建议项同次供给） | P1 | verifyitem / verifyapi / 前端 spec |
| 二 | REQ-V-018/005/006 | P1 | verifysuggest / verifyapi / 前端 spec |
| 三 | REQ-V-009/010/011/014/017 + REQ-V-003 | P1+P2+P3 | verifyitem / evidencefile / verifyreplay |
| 四 | REQ-V-012/013（external 建议项转台账） | P2 | verifyreq |
| 五 | REQ-V-008/015（建议项不计数门禁） | P1 | verifyapi（门禁）/ verifysuggest |
| 六 | 红线回归 | P1 | verifyitem / m3chain / statestore |

## 演示收益

- 拆案不再考验用户：系统按核查手册把"该查谁、查什么、用什么渠道"预填成建议卡，人只做采纳/改/忽略；建议可溯源到规则与假设声明，审计链同时留着"系统建议了什么、人采纳或改写了什么"。
- 手册覆盖不到的维度（通讯/轨迹），允许 LLM 在 shadow + 人审两闸下补方向，且红线按部署面分档：**local 档本机 Ollama 数据不出机即可用**（relaxed 脱敏、端点闸 DNS 解析后 IP 复核），cloud 档公网出网需显式授权 + strict 脱敏；off 档默认零调用降级。AI 建议同样可以被"已查否"——模型只管提方向，定性权始终在人。
- 固证时审计链里躺着的是"谁在何时对哪条疑点依据哪份书证得出什么结论"的完整证据包，而不是一句"情况属实，予以固证"。
