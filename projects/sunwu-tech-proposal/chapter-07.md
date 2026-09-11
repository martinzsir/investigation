# 第 7 章　安全、权限与可观测性

侦查推演内核处理的是调查数据，对权限、脱敏与可追责有强约束，同时还存在 LLM（大语言模型）与 MCP（Model Context Protocol）两类外部交互面。本章在语义层之上界定权限面，说明"谁能看、谁能做、哪些信息必须遮蔽、每一步如何留痕可回放"，并交代系统故障如何与侦查发现物理隔离。所有强制点均落在具体模块与行号，便于代码审计者逐条核对。本章不重复第 1–6 章已定义的语义层、数据接入、规则引擎与写路径，仅引用其结论。

## 7.1 权限模型总览

权限面以 `AccessContext`（`core/access.py:79`，frozen dataclass）为唯一载体，贯穿全部五条出口：统一读网关 `OntologyReadGateway`、只读 `FunctionExecutor`、写路径 `ActionExecutor`、MCP Server、以及导出通道。任何一次访问都必须携带一个已通过校验的 `AccessContext`，而不是把"当前用户"散落在各模块局部变量里。frozen 意味着上下文一旦构造便不可改写，配合构造期三重校验，把"身份合法性"前移到对象生命周期起点，也避免了在长调用链中被中途篡改而导致审计失实。

字段包括 `operator`（必填非空）、`role="正兵"`、`case_id="default"`、`purpose=""`、`clearance=1`、`network="local"`。其 `__post_init__`（`core/access.py:92`）执行三重校验：operator 为空、network 不在 `(local, isolated, web)` 集合、role 不在 `ROLE_RANK`，任一不满足即抛 `ValueError`。这三道闸门保证后续所有鉴权逻辑可以信任上下文字段有效，无需再各自判空。系统旁路 `system_context()`（`core/access.py:145`）仅用于内核自身编排（`is_system=True`），依旧过同一构造器，不绕开校验。

与 LLM 调用相关的两道边界在 context 上直接暴露：`can_llm_call()`（`core/access.py:113`）在 `network="isolated"` 时返回 `False`；`require_llm_allowed()`（`core/access.py:151`）在其为假时抛 `LLMBlockedError`。这意味着"物理隔离网络禁止任何大模型调用"是代码强制的，而非配置约定。五条出口中凡涉及 LLM 增强的路径，都必须先过此检查；导出通道若要把内容送出隔离环境，同样受 network 字段约束。由此，`AccessContext` 不只是一个身份标签，而是把"环境可信度"与"行为能力"绑定的统一凭证。

## 7.2 角色体系与两把尺子

角色用两把互相独立的尺子刻画，二者在代码注释中明确禁止互比（`core/access.py:40`）。第一把 `ROLE_RANK`（`core/access.py:25`）刻画"行政秩级/动作权限"，取值 `{见习:0, 正兵:1, 偏将:2, 主办:3, human:4, system:99}`；第二把 `ROLE_CLEARANCE`（`core/access.py:44`）刻画"信息密级可见度"，取值 `{见习:0, 正兵:1, 偏将:3, 主办:3, human:3, system:99}`。两把尺子的数值刻意不一致：偏将的 rank=2 但 clearance=3，human 的 rank=4 但 clearance=3，这本身就是"动作权限"与"信息密级"解耦的具象证据。

之所以不能合并成一把"总级别"，是因为"能不能做某个动作"与"能不能看某个密级"是两个正交维度。动作权限管的是写入、迁移、置状态这类行为能力；信息密级管的是对象属性中敏感字段的可见范围。若合并，会推导出"能看就能改"或"能改就能看"的隐含等式，而侦查场景恰恰要求二者分离：一个高密级可见的分析员未必有写权限，一个被授权发起动作的脚本未必该看到全部明细。注释中"禁止互比"正是对这种耦合的防御——比较权限时只能用 rank，比较密级时只能用 clearance，混用会在代码评审阶段被拦截。

`jian_clearance_for_role`（`core/access.py:54`）与 `can_see_jian_types`（`core/access.py:59`）进一步把 clearance 下沉到"五间"维度：不同间类（因间/内间/反间/死间/生间）带有 `default_clearance`，角色密级低于对象密级即不可见。这把"角色—密级"映射到业务语义层，使权限判定在第 3 章定义的语义对象上自然成立。`system=99` 是唯一的旁路值，仅供内核编排使用，业务操作人永远不应以此为 operator，否则会被第 6 章的 `FORBIDDEN_OPERATORS` 拦截。

两把尺子还分别应对不同审计视角：rank 主要回答"这次操作是否被授权"，用于 ActionExecutor 与 MCP 写通道的前置判定；clearance 主要回答"这个角色看到了多少"，用于网关属性级遮蔽与 `can_see_jian_types` 的间类过滤。把二者分开，也使权限变更的影响面可控——上调某人 rank（赋予动作能力）不会自动放大其可见密级，反之亦然。第 1 章禁令③（不置"已立案"）正是 rank 维度的末端约束：human=4 是能置终态的最高业务秩级，而 system 旁路仅用于内核编排、被 `FORBIDDEN_OPERATORS` 排除在状态机操作人之外，二者在代码层互不越权。这也解释了第 18 章已知风险中"两把尺子认知负担"——数值刻意不一致正是正交性的代价，评审时应以注释为准而非直觉对齐。

## 7.3 三级鉴权

鉴权在语义层之上分三级：`PolicyEngine`（`core/policy.py:49`）提供 `check_object`（对象级，`policy.py:86`）、`check_link`（链接级，`policy.py:102`）、`can_read_property`/`mask_value`（属性级，`policy.py:122`/`129`）。对象级与链接级查不到声明即抛 `PolicyDeniedError`（`policy.py:26`），属性级则进入脱敏而非拒绝——这是"拒绝读取敏感对象"与"返回但遮蔽敏感列"的区分。三级对应"能否访问这张表 / 能否沿这条关系 / 能否看到这一列"的逐层收紧。

这种分层不是冗余，而是对应语义层的三类访问原语：对象是最粗的边界（一张业务表能否被这个角色读），链接是关系边界（沿某条边能否遍历），属性是最细的边界（某一列值能否以明文/半遮出现）。网关作为统一读入口（REQ-002），把三级鉴权收口在 `objects/links/view/prop_indicator` 等少数方法内，调用方无法通过绕过网关直查 `obj_*/lnk_*` 来逃避——第 1 章禁令①（直查拦截 `_FORBIDDEN_TABLES`）在存储层兜底，与权限面的网关收口形成纵深防御。三级鉴权因此既是策略表达，也是不可替代的代码执行路径，而非可被上层随意绕过的约定。

对象级策略在 `policies.json` 中以 `object_policies` 字典承载（`policy.py:79`）。`check_object` 先取 `object_policies.get(name)`，取不到即 `raise PolicyDeniedError`（`policy.py:90`、`:97`）；链接级同理（`policy.py:108`、`:113`）。语义视图 `view()`（`core/gateway.py:135`）额外校验 `access.role ∈ view.roles`，不在名单内抛 `ViewAccessDenied`（`gateway.py:152`），但仍按 base_object 走对象级+属性级策略——视图只是投影，不绕过底层权限。这样即便声明了一个宽松视图，敏感列的遮蔽仍由 base_object 的属性策略兜底。

### 7.3.1 三处 fail-closed 强制点

fail-closed（未声明即拒绝）在本节有三处强制点，构成"安全默认态"的骨架。第一处：`policies.json` 缺失时，`PolicyEngine.__init__` 令 `self.object_policies = {}` 且 `self._missing_file = True`（`policy.py:61`、`:63`），于是任何对象查询都查不到策略而被拒绝，而非放行。第二处：`schema_version != 2` 时装载期直接 `raise ValueError`（`policy.py:69`，对应 REQ-G-016），防止旧格式声明被误当作有效权限。第三处：声明中出现的角色集若不在 `ROLE_RANK` 内，同样 `raise ValueError`。

这三处强制点的工程意义在于：当新增一个对象却忘记配策略、或策略文件损坏、或声明格式漂移时，系统会拒绝访问而不是悄悄放行。对被审计系统而言，"新增即默认拒绝"是可追责的前提——它把配置疏漏暴露成运行时失败，而非埋成数据越权隐患。这也与第 4 章的数据接入路径呼应：新数据源接入的第六步明确要求在 `policies.json` 补策略，漏配会在运行时被 fail-closed 捕获，而不是等到越权访问才暴露。

## 7.4 脱敏策略

属性级脱敏由 `_MASKS` 映射（`policy.py:46`）驱动，含 `partial` 与 `full` 两档。`mask_partial`（`policy.py:38`）保留前 3 位与后 4 位、中段以 `*` 填充，例如 `310****1234`；长度不足 8 时整体遮蔽（`policy.py:39` 注释），因为过短的值即便只露前 3 后 4 也可能被反推。 `full` 档直接返回 `***`（`policy.py:46`）。`mask_value`（`policy.py:129`）按属性声明的 `mask` 字段选择函数，缺省走 `full`，即"未显式声明可部分展示的列一律全遮"。

脱敏不仅作用于逐行明细，也覆盖聚合统计。网关的 `value_profile`（`core/gateway.py:221`）与 `distinct_values`（`core/gateway.py:279`）在返回 `samples/min/max` 等统计时，会连同原始值样本一并遮蔽——避免"不直接返回明细、却把样本泄露出去"的旁路。这与第 1 章禁令②（不把原始明细搬进上下文）形成闭环：Function 只回聚合与溯源 ID，聚合里的样本同样受 `mask_value` 约束。`prop_indicator`（`gateway.py:300`）等派生指标亦走同一脱敏管线。

需要强调的是，脱敏是属性级策略的结果而非调用方自觉。`apply_row_masks`（`policy.py:136`）对整批行统一施加，调用方无法"按需跳过"某一列；`can_read_property` 返回布尔，决定该列是否进入脱敏前的可见判定。这样即便上层代码忘记遮蔽，网关返回的列集已经过策略裁剪，原始敏感值不会以明文形态出现在出口。对于 `value_profile`/`distinct_values` 这类会回显样本值的接口，遮蔽在返回前统一完成，杜绝了逐接口遗漏。

## 7.5 审计哈希链

审计由 `AuditChain`（`core/audit.py:115`）实现，是一条 SHA-256 链式结构。链首 `_GENESIS_HASH = "0"*64`（`audit.py:21`），之后每条事件在 `append`（`audit.py:178`）时取上一条的 `signature` 作为 `prev_hash`（`audit.py:188`），再对"全部字段 + prev_hash"计算 `sha256` 得到本条 `signature`（`audit.py:96`、`:100`）。因此任何一条历史记录被篡改，都会使其后整条链的 `prev_hash` 衔接失败、签名重算不一致，篡改无法局部隐藏。

链完整性校验由 `chain_verify`（`audit.py:223`）与 `chain_integrity`（`audit.py:256`）完成，后者会列出 `broken_links`（断链或签名不一致的事件 ID），供审计者定位疑似篡改或丢链。`signature` 算法 `_compute_signature` 是双后端共用的纯函数，因此 `backend="duckdb"`（案件版本库）与 `backend="sqlite"`（Web 业务状态库 `state.sqlite`）下，同输入产生等价的 `signature` 与 `root_hash`（`audit.py:378`），两条链路可交叉验证——Web 侧写入的状态库与 CLI/MCP 侧写入的案件库能相互印证，防止任一侧单独被绕过。

版本锚点缺失是现实中的常见情况——sqlite 后端没有 `meta_ontology_state` 表，拿不到本体版本号。此时 `append` 不崩、不改动表结构，而是在 `after` 状态内打 `anchor_status=missing`（`audit.py:194`）、并落 `version_anchor_missing` 诊断（`audit.py:168`、`:173`）。这保证了"锚点缺失"是可观测的降级而非静默通过，符合第 2 章原则"失败要留痕，不许静默"。由于不改表结构，存量审计记录在版本机制演进后仍可继续追加，不会因锚点格式变化而作废。

链的抗篡改性还依赖 `seq` 自增序与签名冗余：即便攻击者尝试替换中间某条记录，由于 `prev_hash` 引用上一条签名，后续每条记录的签名都会失配，`chain_integrity` 一次性可定位首条断点。双后端等价则意味着 Web 侧（sqlite）与内核侧（duckdb）任一侧单独被改都无法自洽——交叉校验任一端即可发现另一端被篡改，这正是把审计做成"双向可证"而非"单点可写"的设计意图。对于离线部署场景，即使没有 Web 状态库，内核侧 duckdb 审计链本身也已具备逐条可验的完整性，不依赖外部组件即可对外举证。审计链与事件总线（7.6）在职责上互补但都不进入 findings 主列表，这与 7.7 的物理分离原则一致：审计记录的是"谁改了什么状态"，事件是"发生了什么动作"，二者都属于系统自身行为的证据，而非侦查发现。把系统行为与侦查发现分库分表，既避免健康度噪声污染交叉升格，也让审计链在司法鉴定场景下可被独立提取与验证。

## 7.6 事件总线

事件总线 `EventBus`（`core/event_bus.py:92`）基于 DuckDB 三张表：`event_log`（自增 `seq` 主序）、`event_dead_letter`（死信）、`event_idempotency`（幂等键）。系统中目前登记 **17 类事件**，覆盖构建、规则运行、动作派发、审计、诊断等生命周期节点，使各模块能以解耦方式广播"发生了什么"而不必了解消费者。`publish`（`event_bus.py:114`）将 `payload` 的 `sha256` 作为 `payload_hash`（`event_bus.py:125`）写入，既可用于完整性核对，也作为幂等去重的输入。

消费失败不会丢失：handler 异常时被写入 `event_dead_letter`（`event_bus.py:162`、`:231`），后续可盘点重投，调用方原先 `except: pass` 的静默吞错被改为落 `event_publish_failed`/`event_dead_letter_summary` 诊断（`run_health.py` 对应 kind）。同一 `(idempotency_key, handler_name)` 用 `INSERT OR IGNORE` 去重（`event_bus.py:147`），保证幂等消费。环检测 `detect_cycle`（`event_bus.py:240`）沿 `causation_id` 逐跳回溯，当深度超过 `max_depth=10` 判定为循环（`event_bus.py:253`），避免因果链无限递归导致重放雪崩。

重放 `replay`（`event_bus.py:179`）从指定事件之后顺序重投所有事件，并为每个事件生成 `replay-` 前缀的新幂等键（`event_bus.py:207`），确保重放与首次执行互不冲突、可重复演练。`list_events`（`event_bus.py:258`）支持按类型与条数检索，便于事后取证。事件总线与审计链是互补的两套可观测设施：审计链记录"状态如何变化且不可篡改"，事件总线记录"发生了哪些动作、可否重放"。二者都不依赖 LLM，在内核零依赖前提下即可运行，满足离线部署要求。

17 类事件的设计遵循"凡是状态跃迁与可观测动作都留事件"的原则，使下游消费者（如审计、降级、人审闭环）可用订阅而非轮询的方式响应。例如 `ontology.materialized` 事件驱动人审闭环（REQ-016）的增量重算，`dispatch_failed` 类事件进入 `event_dead_letter` 后可被对账模块（REQ-014）重试直至 `manual_409`。`detect_cycle` 的 10 跳上限不是业务规则而是防呆：正常因果链深度远低于此，超过即说明 causation_id 回填出现环路 bug，应告警而非无限重放。事件总线的存在，让"可观测性"从被动查日志升级为主动的、可重放的事件流，也为调试与取证提供了时间有序的因果链。

## 7.7 运行健康度与降级协议

运行期所有非致命异常与降级一律落 `run_diagnostic` 表，由 `RunHealth`（`core/run_health.py:79`）统一管理，`record` 方法校验 `kind` 必须在 `KINDS` 元组内（`run_health.py:105`），否则抛 `ValueError`，防止拼写错误的诊断种类悄悄入库。`KINDS`（`run_health.py:28`）实际登记 **28 类**诊断，可归为几族：构建期降级（脏值/缺列/隔离/复合值/清洗剔除率）、实体解析降级、事件/派发生命周期失败、覆盖与审计缺口、质量门与合规告警、以及手动运行印记。所有接入点签名均兼容 `health=None`，此时经 `get_health`（`run_health.py:261`）返回 `NullRunHealth`（`run_health.py:236`）做空操作，保证既有不传参调用零行为变化。

与 findings（侦查发现主列表）**物理分离**是本节的核心设计决策。诊断进 `run_diagnostic`，枚举为产物的"健康度"小节；语义层重建编译器只 `DROP obj_*/lnk_*`（`run_health.py:16` 注释），不会触碰运行期表。之所以必须分离，是因为系统故障（如脏值降级、缺列、派发失败）若在 findings 里，会被当成一条真实的侦查发现，进而触发第 5 章的五间交叉升格——一次数据接入异常可能错误地"升格"为立案依据候选，后果严重且难以回溯。物理隔离使"系统说不清楚"与"系统发现了什么"成为两条不可混淆的数据流。

降级协议的整体基调是"降级但不静默、留痕但不污染"：每一项降级都带 `severity`（info/warning/critical，`run_health.py:59`）与可下钻样本，且样本本身经脱敏（如 `compliance_violation` 只记对象.属性+代理键+违规码，原始值不入库）。这要求所有接入点默认传入 `health`，而历史遗留调用以 `NullRunHealth` 兜底，不会因缺失 health 而抛错。`diagnostic_run`（`run_health.py:56`）保证即便是零问题的运行也会留一条 info 印记，使"无诊断"本身也可被观测、可被证明不是"静默吞错"。

## 7.8 内容寻址溯源

溯源由 `core/row_uri.py` 提供内容寻址能力，使"某条 finding 来自哪一行原始数据"可被精确回捞。行 URI 形态为 `dataset@version#partition/rowid`（`row_uri.py:6`、`:81`）：`dataset` 标识源表，`version` 为本体版本（hex），`partition` 为数据分区（bootstrap / 增量 partition_id / incremental），`rowid` 为行内容地址 `sha256(dataset + 有序 col=value)[:16]`（`row_uri.py:12`）。内容不变则 `rowid` 不变，旧 URI 永远 resolve 回旧内容，这正是"内容寻址"相比行号寻址的抗漂移之处。

构造与解析由 `make_row_uri`（`row_uri.py:71`）与 `parse_row_uri`（`row_uri.py:84`）完成，二者均强制各段非空、不含保留分隔符、`version/rowid` 须为 hex，形态非法抛 `MalformedUriError`。`row_id_for`（`row_uri.py:109`）与 `uri_for_row`（`row_uri.py:122`）从一行数据算内容地址；`snapshot_source_rows`（`row_uri.py:145`）在构建期把行内容写入 `row_archive`、把构建索引写入 `row_build_index`（两表定义见 `row_uri.py:16`、`:20`），均用 `INSERT OR IGNORE` 去重，因此同内容多次构建不会重复归档。

回捞由 `resolve_row_uri`（`row_uri.py:252`）完成：先据 `row_build_index` 校验该行是否属于指定 `version`（`row_uri.py:281`，否则报"不属于该版本"），再从 `row_archive` 取 `content`（`row_uri.py:285`）。这实现了第 1 章禁令②（不把原始明细搬进上下文）——Function 只返回聚合结果与溯源 ID，需要细看时再用 URI 回捞，且回捞出的仍是受密级约束的数据。内容寻址的另一个好处是跨构建版本的可比对：同一 `rowid` 在多个 `version` 的 `row_build_index` 中出现，即可证明"这行原始数据在 N 次构建里都被消费过"，为溯源与对账提供稳定锚点。

## 7.9 规则度量与推翻率告警

规则运行时度量由 `core/metrics.py`（REQ-030）承载。`ensure_rule_run_metric`（`metrics.py:34`）建表；`record_run`（`metrics.py:77`）记录每次运行逐规则结果；`verdict_backfill`（`metrics.py:101`）回填人工裁决。关键设计是 `rule_version`（`metrics.py:66`）= `hash(rule_id + rule_text + 排序后的 params)`，使指标按"规则语义版本"而非"规则 ID"分层——同 ID 但判据/参数变更会被视为不同版本，避免历史指标错配到新规则。版本分层后，调整阈值或改措辞都不会污染旧版本的表现基线。

聚合视图提供三个比率：`hit_rate`（`metrics.py:133`，命中/评估）、`precision_estimate`（`metrics.py:141`，以人工裁决为正的精确率估计）、`override_rate`（`metrics.py:150`，= `override_count / evaluated`）。其中推翻率（override_rate）直接对应质量信号：当人工裁决反复推翻某规则命中，说明该规则在当前案件上产生过多误报或已不适用。`alert_override_rate`（`metrics.py:252`）遍历指标，对超阈规则落 `override_rate_alert`（warning）诊断；阈值取自 `thresholds.json` 的 `alerts.override_rate_max`，读不到回落 `0.5`（`metrics.py:246`）。把"规则被推翻"做成可告警信号，使规则质量从一次性评审变成持续可观测的指标。

度量本身也受权限约束：`list_metrics`（`metrics.py:165`）按 `access` 过滤，敏感规则（如 R3/R4）对低权限调用方只返回数值量级摘要（`metrics.py:230` 注释），不暴露可被反推的明细。这样"规则表现好不好"可观测，但"哪条线索被哪条规则命中"的敏感关联不越权外泄。`precision_estimate` 依赖人工裁决回填，在裁决样本不足时返回 `None` 而非臆造数值，避免用稀薄样本给出误导性高精度。

推翻率告警还反向驱动规则生命周期：当某规则 `override_rate` 持续超阈，结合 `precision_estimate` 走低，可作为将其移入 `retired` 状态（见参数治理 REQ-032 的 draft/shadow/production/retired 四态）的量化依据，而非仅凭主观判断。度量因此不只是"复盘报表"，而是把第 5 章规则引擎的"判据质量"纳入持续反馈闭环——规则上线后表现可观测、可比较、可退役，避免僵化的误报长期污染 findings。版本分层（`rule_version`）则保证这种比较始终在同语义版本内进行，不会把"改判据后的新规则"与"旧规则"的命中率混为一谈。

## 7.10 MCP 边界与 LLM 治理

MCP Server 暴露 13 个工具（`scripts/mcp_server.py:_TOOL_IMPL`，`mcp_server.py:933`），按风险分三档。第一档为纯只读侦察类，无副作用、只附摘要：状态源强制走 DuckDB 而非 JSON 快照的 `clue_list`（`mcp_server.py` 注释，避免读到过期镜像）、`scan_anomaly`/`cross_jian`/`graph_overpass`/`function_list`/`function_invoke`/`rule_list`/`review.list_pending`（不附 evidence 明细）/`action.status`。第二档为受控写，必须经人工坐落：`clue_transition` 拒 `system/ai/agent` 操作人、置"已立案"必带 `legal_basis` 且只能从"已固证"迁移（`mcp_server.py:541`、`:583`），`run_pipeline` 需 `confirm=true`（`mcp_server.py:625`）。第三档为 Agent 唯一写通道 `review.submit_proposal`（`mcp_server.py:302`、`:844`），只建 `status=draft` 提案、永不自动生效、绝不改线索状态。

人机边界由代码而非提示词保证：`clue_transition` 中若 operator 属 Agent 身份直接报错（`mcp_server.py:555`）；`review.get_evidence` 对正兵及以下只返回类型与计数摘要，阈值取 `ROLE_RANK["偏将"]`（`mcp_server.py:735`）。所有返回体经统一封装强制挂 `needs_human_review` / `定性_policy` / `disclaimer`（`mcp_server.py:377`–`:379`），使"AI 不给出定性结论"成为协议层事实而非模型自觉。即便上游模型越权生成了定性表述，网关封装层也会附上 `定性_policy` 声明，下游消费方据此识别这是候选而非结论。

LLM 治理分五块。其一 `core/llm/redact.py`（REQ-038）出网脱敏，PII 正则**顺序敏感**：身份证 `(?<!\\d)\\d{17}[\\dXx](?!\\d)` 必须先于银行卡 `\\d{16,19}`（`redact.py:33`–`:40`），否则 18 位身份证会被 16–19 位银行卡规则吞掉，导致身份证末位被当作卡号中段保留而前段泄露；此外未走脱敏通道的输入（缺 `redaction_hash`）或复扫仍检出 PII 一律禁止出网（`redact.py:360`、`:369`）。其二 `core/llm/guard.py`（REQ-039）提示注入防护：`scan_text`/`wrap_untrusted`（把外部内容包成 `untrusted_content` 帧）/`scan_bundle`/`sanitize_candidate`/`assert_no_status_change`（候选含状态变更即拦截，`guard.py:198`）/`raw_evidence_fragment`（原始证据片段只供人工、不进模型上下文，`guard.py:224`）。

其三 `core/llm/explain.py` 的 `_QUALITATIVE_WORDS` 定性词黑名单（`explain.py:29`），含"涉嫌/确认/违法/立案"等，命中即剔除或拒绝输出（`explain.py:153`、`:184`），把"不下定性结论"做成编译期正则而非依赖模型守纪律。其四 `core/llm/fallback.py`（REQ-040）一键降级 + 影子模式：`llm_enabled=false` 即零模型调用（`fallback.py:29`），影子模式仅产 proposal/日志/审计、不进生产 finding；`LLM_OWNED_TABLES`（`fallback.py:18`，`{proposal, llm_call_log, audit_chain}`）界定 LLM 可写的表集合，`shadow_diff` 断言其余表行数零变化（`fallback.py:69`、`:78`），从存储层证明"LLM 只写它自己的表"。其五 `core/proposal.py`（REQ-033）对提案做七项硬校验（信封 jsonschema / 函数白名单 / 参数强类型 / 证据 URI 合法等，`validate_proposal` 于 `proposal.py:148`），`confidence_only_sorts`（`proposal.py:241`）断言"不同 confidence 下命中集合不变"——confidence 只用于排序，绝不改变定性。

综上，MCP 与 LLM 两层都遵循同一原则：模型是可选增强而非决策主体，所有写动作、所有定性、所有出网文本都受代码级闸门约束，且任意一层失效都能一键降级到确定性内核。第 1 章三条禁令与第 2 章六条原则在本章得到可审计的落点：权限 fail-closed、脱敏强制、审计不可篡改、故障与发现物理分离，共同构成内核的安全底座。

三档分级的底层逻辑是"写能力逐级收窄"：只读档零副作用、受 DuckDB 真值源约束；受控写档把动作锁在人工决断（legal_basis/confirm）之内；Agent 写档则被压缩到只能投递草稿。影子模式进一步把 LLM 的"产出"与"生效"切开——`shadow_diff` 以表行数快照断言 LLM 仅写入 `LLM_OWNED_TABLES`、其余生产表行数零变化，从存储层证明降级可逆、增强可回退。这种"能力越小、闸门越硬"的收敛结构，使外部交互面无论多宽，最终都收敛到确定性内核这一唯一可信基；即便 MCP 工具或 LLM 层被完全关闭，侦查推演仍能在零外部依赖下完整运行，满足物理隔离部署的硬约束。
