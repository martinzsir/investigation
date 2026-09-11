# 第 6 章　线索生命周期、处置与写路径

本章聚焦系统的写入侧（write path）：从规则引擎产出的 finding 转换为线索（clue）之后，如何被正兵（人类处置者）跟踪、处置、固证、立案，并最终闭环回到语义层与案例库。与第 1–5 章不同，本章不重复术语、架构与规则计算的细节，而是把笔力集中在"谁有资格写、写之前经过哪些校验、写之后如何对外确认与对账、异常与暂缓线索如何被隔离"这一条纵贯线上。核心命题是：在确定性侦查内核里，写操作不是功能的附属动作，而是受治理约束的第一类公民。所有写路径收敛到唯一的 `ActionExecutor`，所有状态迁移受状态机与角色边界双重约束，所有对外回写经发件箱（outbox）与对账（reconcile）保证不丢、不重、不假装在途。

## 6.1 线索模型与状态机

线索模型以 `LineageClue`（`core/registry.py:147`）为内存标准单元，承载假设链（assumption_chain）、数据溯源（source_rows）、间类归属（jian_types）与处置状态（status）。处置状态由 `ClueStatus`（`core/registry.py:38`）以五态常量表达：待查（PENDING）、查证中（VERIFYING）、已排除（EXCLUDED）、已固证（CONFIRMED）、已立案（FILED）。状态不是自由字符串，而是受状态机约束的有限集合，任何变更都必须经由 `set_status` / `set_filed` 两个方法（`core/registry.py:180`、`:210`），禁止直接赋值 `.status`，以保证审计链（audit_log）完整。这一设计把"状态从哪来、经过谁、为什么变"固化进数据结构，而不是散落在各处的过程代码里。

状态机的核心是一张合法迁移表 `ClueStatusMachine._TRANSITIONS`（`core/registry.py:73`），它定义了五态之间的可达关系。待查可前进到查证中、已排除或已固证；查证中可回退到待查，也可走向已排除或已固证；已排除并非不可逆，可因新证据重开回待查，这是侦查中"排除结论被新线索推翻"的真实需要；已固证可落到已排除或已立案；已立案是封闭终态，不再外溢。迁移表并非凭空写死在 Python 里——正式包从 `states.json` 读取（见 `ClueStatusMachine.transitions_for`，`core/registry.py:82`），默认表只是回落值，确保状态语义随案件包声明而演进。

| 当前状态 | 可迁移到 | 说明 |
|---|---|---|
| 待查 | 查证中 / 已排除 / 已固证 | 初始态，正兵接手或机器初判 |
| 查证中 | 待查 / 已排除 / 已固证 | 核查中可回退或升级 |
| 已排除 | 待查 | 排除可因新证据重开 |
| 已固证 | 已排除 / 已立案 | 稳定证据后走向终局 |
| 已立案 | （无） | 受控终态，仅人类可置位 |

五态中最关键的是"已立案"的专属地位。系统通过两条互锁的防线把它锁死在人类手里：其一，`core/access.py:37` 定义 `HUMAN_ONLY_STATUSES = frozenset({"已立案"})`，并在 `access.can_transition`（`core/access.py:140`）中硬性拦截非人类角色向该态的迁移；其二，`LineageClue.set_filed`（`core/registry.py:210`）要求当前状态必须处于已固证或已立案，且必须携带法定依据（legal_basis），`set_status` 则直接拒绝把目标设为已立案（`core/registry.py:189`）。由此，机器、Agent、自动化占位名无论如何都无法越过这条红线——这与第 1 章"机器只做计算与提示，绝不置已立案"的边界声明在代码层完全对齐。

代码层还提供 `DisposalBoard`（`core/disposal.py:49`）作为正兵处置看板，统一封装 `transition / verify / exclude / confirm / file / persist / restore / report` 等方法。它内部持有一个 `ActionExecutor`（`core/disposal.py:64`），所有状态变更都经由此执行器，而非直改线索对象，从而在看板这一便利入口也守住了"写路径唯一"原则。每一条状态变更都会生产一条 `StatusAuditEntry`（`core/registry.py:104`），记录从态、到态、操作人、备注与关联的事件号（event_id），使处置过程可逐条追溯。

## 6.2 ActionExecutor：唯一的写入口

"写路径唯一"是本内核的设计原则之一（见第 1 章全局规范与 `reference-keypoints.md` 第 2 节），其工程落点就是 `ActionExecutor`（`core/action_executor.py:67`）。构造函数签名为 `ActionExecutor(store, pack, access, health, sink)`，五个依赖分别承担存储后端、案件包、权限上下文、运行健康度与可选的 Web 写后端（sink）。它之所以是"唯一"的写入口，是因为 Function 层被强制只读（SQL 白名单只放行 `SELECT`/`WITH`，见第 5 章），而所有会改变线索状态或产生副作用的动作，无论来自 `run_all` 主链路、`clue_transition` MCP 工具还是两阶段提交，最终都汇聚到这一个类。这种收敛带来的直接收益是：角色校验、状态机校验、参数校验与审计链被集中实现一次，任何新增的写动作都无法绕过它们。

`ActionExecutor` 提供两条执行路径。路径 A 是即时执行 `execute()`（`core/action_executor.py:122`），用于 `run_all` 与 `clue_transition` 这类"提交即生效"的场景，行为保持既有；路径 B 是两阶段提交（REQ-012），即 `submit → approve → dispatch → mark_confirmed` 的异步闭环，用于需要"机器提议、人类批准"的治理场景。两条路径在落本地变更时共用 `_validate`（`core/action_executor.py:131`）与 `_apply`（`core/action_executor.py:169`），校验逻辑因此只有一份真相。

值得把"写路径唯一"的代价与收益说清楚。代价是任何写功能都必须先定义 `actions.json` 中的动作声明，并经由执行器；这比在业务代码里随手 `UPDATE` 一行要啰嗦得多。收益则体现了确定性内核的根本立场：写操作天然带上角色校验、状态机校验与审计链，而不是依赖每个调用方"记得调用审计"。在侦查这类强合规场景下，把约束集中到一处、让旁路无处可走，远比把约束分散到 N 个调用点、再寄希望于代码审查更可靠。这也是为什么 `actions.json` 被归到八段声明中的"写路径"段——写动作本身是一种需要被声明、被编译、被校验的"数据"，而非散落的函数调用。

五步校验是 `_validate` 的主体，任何一步失败都即时抛错，绝不带病进入写操作。第一步是角色校验（`core/action_executor.py:133`）：当动作声明 `requires_role="human"` 时，若操作人是占位名（`_is_placeholder_operator`，`core/action_executor.py:28`），即空串或被 `FORBIDDEN_OPERATORS = {system, ai, assistant, model, bot, auto, llm}`（`core/action_executor.py:25`）命中的名称，直接抛 `ValueError`。这一步堵死了 Agent 或自动化以"正兵"身份伪造写操作的通道。第二步是必填参数校验（`core/action_executor.py:139`）：`file` 动作的 `legal_basis` 属于强制字段，缺失即拒，这是"已立案必须援引法定依据"在参数层面的硬约束。第三步是状态机校验（`core/action_executor.py:147`），调用 `ClueStatusMachine.validate` 阻断非法迁移。第四步是 `only_from` 显式收紧（`core/action_executor.py:150`）：在状态机允许的来源之上，动作可进一步声明"仅能从某态发起"，例如固证（已固证）仅允许从查证中发起，待查不可直接越级固证。第五步是权限上下文一致性（`core/action_executor.py:158`）：非 system 会话必须满足 `access.can_transition`，且操作人须与 `AccessContext` 中的主体一致，防止以他人名义代写。

动作本身的声明类型是 `ActionSpec`（`core/ontology.py:170`），字段含 `name / target_status / allowed_from / parameters / requires_role / side_effects / terminal / only_from / description / title`。这里有一处关键的反向派生设计：`allowed_from`（该动作允许从哪些状态发起）并不在 `actions.json` 里重复声明，而是由 `states.json` 的迁移表**反向派生**——`_reverse_reach_from`（`core/ontology_loader.py:1661`）遍历包迁移表，收集所有"一步可达目标态"的源状态，在 `_load_actions`（`core/ontology_loader.py:1710`）处赋值给 `ActionSpec.allowed_from`。这样做的好处是状态语义只有一个事实来源：改 `states.json` 的迁移表，所有动作的 `allowed_from` 自动随之变化，避免两份声明漂移。副作用同样受白名单约束，`ALLOWED_SIDE_EFFECTS = {set_clue_status, create_decision, merge_entity, dismiss_review}`（`core/ontology_loader.py:70`），任何未注册的副作用在装载期即报错（`core/ontology_loader.py:1693`）。

## 6.3 两阶段提交

两阶段提交（REQ-012）解决的本质问题不是分布式事务，而是治理诉求：机器可以提议一个处置动作，但只有在具名人类批准后才能生效。状态机在 `action_request` 表上推进为 `proposed → approved → dispatching → pending_receipt → confirmed`（另含 `dispatch_failed` 与 `dead_letter` 失败分支，见 `core/action_executor.py:13` 模块文档）。

阶段一是 `submit()`（`core/action_executor.py:192`）：它先跑完整的 `_validate` 前置校验，不合格连登记都不做；通过后把动作登记为 `status='proposed'`，**不执行任何变更**，只写入 `action_request` 草稿。它具备幂等性——幂等键由 `_default_key`（`core/action_executor.py:310`）生成，形态为 `f"{action}:{clue}:{sha256(...)[:16]}"`，相同键重复 `submit` 返回既有 `action_id`（AC5），避免同一提议被登记两次。阶段二是 `approve(action_id, operator)`（`core/action_executor.py:222`）：它强制要求 operator 为具名正兵，占位名直接拒（`core/action_executor.py:224`），将状态从 `proposed` 推到 `approved`，并落 `approved_by` 与 `approved_at`，把"谁批准"钉进记录。阶段三是 `dispatch()`（`core/action_executor.py:240`）：若动作未被 `approve` 就进入派发，抛 `NotApprovedError`（`core/action_executor.py:248`），这保证了"未批准不生效"的硬约束；正常路径下它先本地提交（走 `_apply` 改线索状态、触发副作用），再把回写任务入 `Outbox`，状态推进到 `pending_receipt`。最后是 `mark_confirmed(action_id, external_id)`（`core/action_executor.py:295`），在外部业务系统回执唯一业务号后被调用，置为 `confirmed`。

这套机制最值得强调的工程取舍是"派发 fail-closed"（REQ-G-020）。当 `Outbox` 因故不可用时，`dispatch()` 不会停留在"dispatching 进行中"去假装动作还在路上，而是显式置 `dispatch_failed` 并落一条 `critical` 级诊断（`core/action_executor.py:279`）。这是对"未声明即拒绝、失败要留痕"原则的直接贯彻：任何写操作的中间态都必须可被观测与重放，绝不能让一个看不到落点的派发被误读为"已发出"。

把"机器提议、人类批准"拆成两阶段，而不是简单地给 `execute` 加一个 `operator` 参数，表面看增加了状态与表结构，实则把治理意图变成了可持久化、可回放的状态机。一个 `proposed` 的草稿即使进程崩溃也不会丢失，重启后仍能由具名正兵 `approve` 再 `dispatch`；而 `NotApprovedError` 与具名 `approve` 的强制要求，堵死了"自动化以人类名义批量生效"的隐患。这种设计在某种程度上是牺牲了即时性来换取可问责性——而侦查系统的第一优先级从来不是吞吐，是可追溯。对应的写入侧事件也贯穿全链路发布——`action.submitted`、`action.approved`、`action.dispatched`、`writeback.confirmed` 由 `_publish`（`core/action_executor.py:340`）经事件总线发出，落盘失败仅留痕不阻断主流程（REQ-G-004）。

## 6.4 发件箱、回写与对账

本地提交与外部确认之间隔着一道可靠边界：`Outbox`（`core/outbox.py`，REQ-013）。`dispatch()` 把待回写 payload 落库，由 `WritebackDispatcher` 异步取出发送，状态机为 `queued → sent → confirmed`，失败分支 `failed → dead_letter`。幂等键稳定为 `wb:{action_id}`（`core/outbox.py:58`），同一动作重复入队返回既有 `outbox_id`，确保外部台账不会因为重试而产生重复记录。`enqueue` 与 `list_by_status`（`core/outbox.py:55`、`:75`）构成发件箱的基本存取契约。

对外回写的抽象是 `WritebackAdapter` Protocol（`core/writeback.py:48`），仅含 `dry_run / send / fetch_status` 三个方法，且签名中刻意不出现 `conn / store / board`——从接口层面保证适配器拿不到本体连接，无法反向读取未授权数据（AC5）。`StubLedgerAdapter`（`core/writeback.py:56`）是本地 JSON 台账的 P0 实现，支持故障注入（fail / conflict / 不返回业务号）以覆盖各类外部异常。一个关键定义是"外部成功"的标准：`Receipt`（`core/writeback.py:31`）中的 `external_id` 才是成功凭证——HTTP 200 但无业务号不算成功，`WritebackDispatcher._send_one`（`core/writeback.py:146`）会把它停在 `pending_receipt`（`core/writeback.py:169`），绝不冒进置 `confirmed`（AC4）。`DryRunResult` 与 `ExternalStatus`（`core/writeback.py:25`、`:40`）作为不可变结构，承载 dry-run 与状态查询的结果。

对账由 `Reconciler`（`core/reconcile.py:36`，REQ-014）负责，它面对的是"外部不可靠"这一现实。`MAX_ATTEMPTS = 5`（`core/reconcile.py:32`），退避序列 `BACKOFF_SECONDS = [60, 300, 1800, 7200, 28800]`（`core/reconcile.py:33`），即 1 分钟→5 分钟→30 分钟→2 小时→8 小时的指数退避；达到上限仍失败的转 `dead_letter` 并发告警事件。指数退避而非固定间隔，是为了在外部短时抖动时快速自愈、在外部长时间不可用时又不空耗资源——前紧后松的节奏匹配了"多数故障很快恢复"的真实分布。

特别地，409 冲突**不重试**，直接进人工队列 `manual_409`（`core/reconcile.py:56`），因为冲突意味着外部已有记录，重试只会添乱，应交由人类裁决。`pending_receipt`（200 无业务号）超时后按同幂等键幂等重投，外部不会重复建单。对账差异（本地 `confirmed` 但外部查无此单）只报告、不自动覆盖（`core/reconcile.py:81`），防止外部短暂故障导致本地状态被错误回滚——这条"只报告不覆盖"的克制，正是 fail-closed 在跨系统边界上的延伸。它把"本地以何为真相"这个问题回答得很清楚：本地 `action_request` 的状态是权威真相，外部系统的状态只作为参照，绝不以外部缺失来反推本地应当回退。

## 6.5 `file` 副作用与决策对象

当处置动作是 `file`（置已立案）时，除了状态变更，还会产生一个副作用：创建决策对象（decision）。这由 `_create_decision`（`core/action_executor.py:365`）实现，它体现了对"Web 快速通道"与"CLI/MCP 既有路径"的兼容分层。若 `sink is not None`（即 Web state.sqlite 后端，D-M3-1 设计），决策委托给 `sink.create_decision`，不写语义层 `obj_decision` / `lnk_decision_for`——因为决策是正兵的最终定性产物，不应随语义层 BUILD 重建而丢失。否则走 DuckDB 路径：先 `ensure_runtime_tables` 保证运行时表存在（`core/action_executor.py:374`），再 `INSERT INTO obj_decision` 写入 9 列（`core/action_executor.py:384`）：`decision_id`（形如 `decision_{int(time.time()*1000)}`，`:378`）、`target_status`、`clue_id`、`legal_basis`、`operator`、`note`、`created_at`、`metadata`、`source_rows`，随后 `INSERT INTO lnk_decision_for`（`core/action_executor.py:390`）建立决策与线索的链接。

把决策对象独立成 `obj_decision` 而非仅存在内存里，意义在于它让"为什么立案、依据什么、谁立的、援引哪条法定依据"成为可检索、可审计、可随案例库复用的第一等数据。它也是 6.8 案例库沉淀的原材料之一。与之配套的审计与事件发布在 `_chain`（`core/action_executor.py:89`）与 `_publish`（`core/action_executor.py:340`）中完成：审计链 `AuditChain` 惰性构造并复用同一连接，处置动作（含受控的已立案终态）一旦落链，便写入持久哈希链，解决了此前只写内存 audit_log 导致的"自检空链假阳性"问题（REQ-G-025）。

这里要特别指出主链路上的一个衔接点：在 `run_all.py` 主流程里，`DisposalBoard.persist()`（`run_all.py:294`）把处置状态落 DuckDB 后，紧接着会**重新 `build_ontology` 刷新 `obj_clue`**（`run_all.py:296`）再重算 report（`run_all.py:305`）。这一步看似多余，实则关键——`obj_clue` 是处置状态的语义层快照，如果 `persist` 后不重建，语义表会停留在"全部待查"的旧状态，导致下游按 `obj_clue` 读取的看板与 DuckDB 真实处置状态相互矛盾；而沿用旧 report 会让 `by_status` 停留旧快照。该衔接把"写路径的落地"与"语义层的刷新"显式绑定，避免了两层状态漂移这一典型隐患。

## 6.6 人审闭环与增量重算

人审闭环（REQ-016）回答的问题是：当正兵对一条实体对齐候选做出 accept 裁决后，语义层与规则结果应该怎么跟着变，且变的范围要可控。核心入口是 `apply_accept`（`core/review_loop.py:119`），流程为：先把归并映射落入受保护的 `entity_mapping` 表（`core/review_loop.py:22`，`INSERT OR REPLACE` 保证归并结果不被编译器重建清除），再经 `plan_from_review` 计算一跳影响范围，得到 `affected_rules`，随后用 `materialize_changed` 做增量重物化，并**只重算受影响规则**（`core/review_loop.py:153`、`:164`）。前后两次 `run_rules` 的结果经 `_diff_findings`（`core/review_loop.py:95`）按规则分组做证据集合 diff，产生 `finding.changed` 事件，变化的 finding 被标 `needs_review=True`、`review_round=2`，重新进入二次 review（AC5）。这一机制与第 3 章的增量重建内核直接衔接——`materialize_changed` 的行级 diff 与受保护表逻辑即来自那里，本章不再展开其编译细节，只说明"人审裁决是它的一次具体触发点"。

闭环的边界约束同样严格。`reject` / `defer` 不删证据、不触发重建，仅经 `record_feedback`（`core/review_loop.py:70`）写 `review.decided` 反馈事件（AC3）。这一点的工程含义是：人审的"否定"同样是一种可追溯的处置动作，它不改语义层，但被记录下来，使得后续任何审计都能回答"当时为什么没采纳"。幂等由 `review_applied` 表保障（`core/review_loop.py:34`）：同一 `decision_id` 重复应用会直接返回首次结果（`core/review_loop.py:132`），避免事件重放导致重复重算（AC2）。重算只覆盖 `affected_rules`，与本次裁决无关的规则结果保持不变（AC4），这既保证了结果正确，也把计算成本压在最小必要范围——在规则集扩大的未来，这种"只动受影响部分"的增量思维将直接决定系统的可扩展性。

## 6.7 defer 回捞与异常线索通道

并非所有候选都能立即裁决，暂缓（defer）与异常（anomaly）是两条需要被特殊隔离的线索通道。`DeferredBoard`（`core/deferred.py:166`，REQ-017）管理暂缓任务，登记时必须携带 `wake_conditions`，否则 `ValueError`（`core/deferred.py:80`）——无条件的暂缓等于把线索丢进黑洞。三类唤醒条件按 OR 语义（`core/deferred.py:103` `match_wake`）：`on_dataset`（某数据集/分区到达）、`after`（TTL 到期唤醒）、`evidence_count_gte`（新证据计数达阈值），满足其一即唤醒。一个细腻的护栏是"条件存在但无法解析"的情形：例如 `evidence_count` 字段非整数，`condition_parse_error`（`core/deferred.py:138`）会把它从安静沉睡的 `waiting` 显式转为 `condition_error`（`core/deferred.py:46`）并 critical 留痕，而不是让它永远卡在"条件不满足"的假象里（REQ-G-005）。

异常线索通道（REQ-G-019，`core/anomaly_channel.py`）的定位是防止"系统故障被误当成侦查发现"。它把 `run_diagnostic` 中可转化的静默/缺口类诊断——如 `rule_zero_hit` 中仅 `empty_result_suspect` 子类（疑似失效或被规避）、`function_empty_degraded`、`coverage_gap`——转化为与正常 finding **同构**的异常线索（`clue_from_diagnostic`，`core/anomaly_channel.py:59`）。之所以强调"同构"，是为了让异常线索能复用正常线索的展示与处置看板，而不必另起一套 UI；但它携带的 `source_rows` 恒为空列表，因为它本就没有证据行，溯源走 `diagnostic_ids` 回到 `run_diagnostic`。

这类线索被强制打上不可覆盖的标记：`级别` 恒为"待核实"、`needs_human_review` 恒为 `True`、`is_anomaly` 恒为 `True`（`core/anomaly_channel.py:99`、`:107`）。它**绝不参与五间交叉升格**（REQ-G-019 AC3）：交叉等级的定义是"非空即命中"，若异常也贡献命中，就会出现"缺得越多等级越高"的荒诞结果。结构上异常线索只是内存/产物中的 dict，绝不写入 `obj_*/lnk_*` 语义表（`core/anomaly_channel.py:18`），任何混合线索流在送交叉/升格前必须经 `non_anomaly` / `partition`（`core/anomaly_channel.py:187`、`:192`）过滤。这条护栏的意义超出技术层面：它把"没查到"与"查到了空白"严格区分——前者可能是数据边界，后者可能是对象刻意规避，机器不应把二者混为一谈并自动给出"无异常"的定性，把判定权留给人类，正是系统"不下定性结论"边界在负向信号上的体现。

## 6.8 案例库沉淀与四道质量门

当一条线索经过人审闭环被核验后，其可复用的经验应沉淀进案例库（REQ-031，`core/case_library.py`）。`settle_fragment`（`core/case_library.py:92`）是唯一的沉淀入口，它用四道质量门把住入库质量，任一不过即整体拒绝并一次性收集错误（fail-fast 但信息完整）。

第一道是**终态门**（`core/case_library.py:103`）：线索处置状态必须落在可沉淀的核验结论集合内（默认 `已固证→verified`、`已排除→excluded`，映射取自 `states.json` 的 `outcome` 声明），待查或查证中一律拒绝——未核验不得入库。第二道是**适用条件门**（`core/case_library.py:118`）：`pattern` 必须是非空文本、长度不小于 15 字，且必须包含 `rule_id`，确保沉淀的是"适用条件"而非一句空泛结论。第三道是 **legal_basis 门**（`core/case_library.py:127`）：法律依据必须非空，案例沉淀必须可援引。第四道是**脱敏门**（`core/case_library.py:131`）：`pattern` / `evidence` 经 PII 正则复扫须零命中，且案件知识包 `case_knowledge.json` 中的真实姓名绝不允许出现，必须使用 `当事人#token` 形式——这把脱敏从"生成时"延伸到"入库前再校验一次"，形成双保险。

四门通过后，片段写入 `case_fragment` 表（`core/case_library.py:39`），并携带 `rule_version` 与 `ontology_version` 做版本溯源（AC5）。每次沉淀都会落一条 `AuditChain` 事件（`core/case_library.py:184`），`audit_event_id` 回写进片段行，使案例库同样纳入全局审计链。检索可由 `search`（`core/case_library.py:200`）按 `rule_id` / `outcome` / 关键词组合完成，支撑后续同类侦查的复用与对齐。

四道质量门之所以设得偏严，是因为案例库是"会被未来规则与人员反复引用"的知识资产：没有终态门槛，未核验线索会被当成可靠经验；没有适用条件门槛，沉淀会变成无情境的结论搬运；没有脱敏门槛，真实姓名会顺着案例库二次扩散；没有 legal_basis，案例失去可援引性。四门把"可复用"与"可信赖"绑定在一起，而不是先入库再治理——在侦查语境下，后者几乎注定失控。至此，一条线索从产出、处置、闭环到经验沉淀的完整写路径闭合，且每一步都带着角色约束、状态机约束与审计留痕。
