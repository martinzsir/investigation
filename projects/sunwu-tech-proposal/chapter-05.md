# 第 5 章　规则引擎与 Function 计算层

本章是全书的**技术核心章**。第 3 章已经把"数据是什么、从哪来"编译成 `obj_*`/`lnk_*` 语义层，第 4 章解决了"数据怎么进来、质量怎么把关"。本章回答的问题是：在语义层之上，机器如何把"侦查判据"翻译成可复现、可回放、可追责的确定性计算。围绕这一目标的工程取舍是：判据（写什么）与执行（怎么跑）与计算（算什么）必须彼此解耦，使得"加一条规则"成为一次声明式编辑，而不是一次 Python 代码改动。本章不重复术语、禁令、架构与数据接入，只聚焦检测逻辑的组织方式、执行机制与边界。

## 5.1 三层分离：判据、执行、计算

本章立论的核心，是把"检测到什么"这件事拆成三个互不污染的层：判据层、执行层、计算层。判据层是 `ontology/<pack>/rules.json`（规则手册，Rulebook），它只描述"在什么维度、依据什么自然语言判据、挂靠哪个 Function、传什么参数"——`function` 字段加 `params` 是唯一机器挂钩，机器不解析 `rule_text` 自然语言，只认只读 Function（`core/rules.py:1-15` 的模块头注释明确"本模块不解析自然语言"）。执行层是 `core/rules.py`，它负责按阶段过滤规则、解析阈值、调用 Function、做命中判定、抑制重叠、归类零命中，是一个与具体业务判据无关的通用驱动器。计算层是 `ontology/<pack>/functions.json` 加 `core/functions.py`，前者声明每个计算单元的输入、参数白名单与 SQL 模板，后者提供只读执行器 `FunctionExecutor` 与 Python 实现的注册表 `FUNCTION_IMPLS`。

这一分离的直接收益是：新增一条检测规则，分析师只需在 `rules.json` 写下 `rule_text` 与 `function`/`params`，若已有可复用 Function 则完全不改 Python；即便需要新计算能力，也只是声明 `functions.json` 并在 `FUNCTION_IMPLS` 注册一个函数（装载期校验 `impl_ref` 存在，未知即硬失败）。判据的增删改因此对代码库零侵入。

它也与第 3 章的"八段声明模型"在精神上同构：`rules.json` 属规则手册段、`functions.json` 属能力目录段，二者都只是声明数据；真正的执行器（`core/rules.py`）与计算注册表（`core/functions.py`）是通用 Python 代码，不随业务变。这意味着"加规则"与"改内核"是两条正交的变更流——内核稳定、声明易变，这正符合第 1 章六条原则之二的"声明是数据、实现是代码"。正因如此，第 16 节所述"新数据源接入六步路径"全程不要求改检测器 Python 代码：接表改 `bindings`、加能力改 `functions.json`、写判据改 `rules.json`、补策略改 `policies.json` 即可。

这也就解释了为什么项目里**没有 detectors 目录**：调查规则在架构上不是"每个规则一个检测器类"，而是声明数据经通用引擎驱动。需要澄清的是，`core/` 模块地图的 H 组确实名为"检测器"，但它仅含 4 个同签名 `scan(gateway, ...)` 的数据治理扫描器——`compliance.py`（数据元合规）、`sensitive_scan.py`（敏感列）、`unit_scan.py`（单位口径）、`data_freshness.py`（数据新鲜度），归属 REQ-D 系列，是**数据质量探测器**而非**侦查规则探测器**（`reference-keypoints.md` 第 4 节 H 组）。侦查侧的"detector"已被 `rules.json`+`functions.py` 的声明式范式吸收，因此没有独立的 detectors 目录是设计结果，不是实现缺失。

三层分离带来的第二重收益是阶段隔离与增量重算。`run_rules(store, stage="xu_shi", pack, rule_ids, health, base_dir)`（`core/rules.py:141`）第一道过滤就是 `stage`：只跑当前技能阶段对应的规则，R1–R5 归 `xu_shi`、R6 归 `qi_zheng`，互不污染。`rule_ids` 非空的入口则只重算指定规则（`core/rules.py:168`），这正是第 6 章人审闭环（REQ-016）"只重算 `affected_rules`"的物理基础——改一条判据、只跑这一条、不动其余，确定性来自"输入相同的声明 → 输出相同的执行"，与 Python 代码改动彻底解耦。判据层出错时定位也变简单：finding 自带 `rule_id`/`rule_text`，审计者能直接回到 `rules.json` 的原文，而不必在代码里翻 detector 实现。

## 5.2 R1–R6 规则详解

现有包 `default`（张卫国受贿/串标案）共挂载六条检测规则 R1–R6，全部落在 `xu_shi`（虚实）阶段，唯独 R6 落在 `qi_zheng`（奇正）阶段，承担奇兵拓线职责。`rules.json` 中每条规则都带有 `dimension`（维度）、`jian_types`（间类）、`assumption`（假设编号）、`function`、`params`、`hit_when` 等字段；其中 `hit_when` 决定命中判定方式——`rows_nonempty` 表示 Function 返回非空即命中，`result_hit` 表示读取 `result.hit` 布尔（见 `core/rules.py:26` 的 `_evaluate_single`）。逐条说明如下。

从分布看，六条规则覆盖五个维度中的资金(3条)、通讯、行为、关系、时间各1条，资金维度权重偏高（R1/R2/R6 三条都围绕整数金额，其中 R6 时间维度依附资金判据），这是 `reference-keypoints.md` 第 18 节已记录的已知风险"规则偏少、资金占3条"。所有规则的 `assumption` 字段指向庙算假设 H1–H4，构成"规则命中 → 假设佐证"的回灌链路：R1→H1（收受财物）、R3→H3（密切私下关系）、R5→H2（利益关联）、R2/R6→H4（第三方过桥）。值得注意的是 `excludes` 字段（R1 排除 R2、反之亦然）与 `exclusive_group` 是两套互补机制：前者是声明层意图声明，后者是执行层实际消解逻辑。逐条的事实细节如下，每一条都附带其降级路径，因为降级路径决定了"这条规则在数据源不足时如何优雅退场"而不是崩溃。

R1「季度末整数现金存入」挂靠 `quarter_end_integer_deposits`，维度资金、间类生间、假设 H1，组内 `primary_rule=true`。判据为金额对 `round_unit=10000` 取模为 0 且 `to_raw='现金存入'`（参数 `cash_summary_tokens`），日期距季末边界 ≤ `quarter_end_window_days=15` 天，按季度聚合。它与 R2 同属 `exclusive_group=integer_amount`，承担组内 primary 角色。降级路径：当 `obj_transaction` 缺失或列不符时，由 Function 层抛 `CatalogException`/`BinderException` 触发结构降级为零命中并标 `degraded`，不崩管线（`core/functions.py:641`）。

R2「整数转账聚合（第三方过桥）」挂靠 `integer_transfer_aggregates`，维度资金、间类反间、假设 H4。判据为 `amount % 10000 = 0`，按 `from_raw→to_raw` 聚合单向大额链条，`subject_column=from_raw`。它与 R1 互斥：`exclusive_group=integer_amount` 且 `primary_rule=false`，`overlap_resolution=drop_if_primary_hit`——当 R1（primary）命中时 R2 被抑制（详见 5.7）。降级路径同 R1。

R3「招投标公示期通话频次突增」挂靠 `call_frequency_spike`，维度通讯、间类生间、假设 H3，`hit_when="result_hit"`。判据为头部对端通话数 ≥ 其余对端中位数 ×2。其降级路径最值得关注：当某主体只有单一通话对端、无法计算常态中位数时，函数自动降级为绝对阈值 `absolute_threshold=30` 并标 `is_degraded=true`（`core/functions.py:176-185`），且 `call_pair_coverage` 可独立诊断"是否仍运行在不可靠的绝对阈值模式"。

R4「二人公示期轨迹同框」挂靠 `co_located_pairs`，维度行为、间类生间。它直接 `SELECT lnk_co_located`（不同主体同地点 ±1 天），`subject_column=person_1`，无参数。降级路径：语义链接未编译或为空时结构降级为零命中。

R5「工商登记利益关联」挂靠 `org_interest_links`，维度关系、间类因间、假设 H2。`params` 为空，判据由 `case_knowledge.json` 驱动：遍历 `obj_org` 的 `legal_rep`/`relation` 是否命中知识包中的 `subject_aliases` 或未过期 `relation_assertions`（人名**只**合法存放于 `case_knowledge.json`，函数与规则代码中不写任何人名，`core/functions.py:477`）。降级路径有两个：知识包缺失→`config_missing` 零命中；`obj_org` 缺失→结构降级。

R6「中标-资金时间窗碰撞」挂靠 `time_window_collision`，维度时间、间类反间、假设 H4，落在 `qi_zheng` 阶段。判据为 `lnk_time_window JOIN obj_bid_project`：金额 `%10000=0` 且 `owner_raw NOT LIKE '%公司%'`（排除对公，只留个人），按 `offset_days` 排序，`subject_column=资金主体`。降级路径同其余 SQL 实现。

需要特别警惕一处术语陷阱：代码注释里出现的 R5/R9/R13 等是 **REQ 需求编号**，不是检测规则编号；本系统的检测规则只有 R1–R6（`reference-keypoints.md` 第 6 节警告）。写作与审计时不得混淆。

## 5.3 11 个 Function 与两类实现

计算层共 11 个 Function（Function），分两类实现：4 个 SQL 实现、7 个 Python 实现。这条分界不是随意的，而是由"计算能否用单条只读 SQL 表述"决定——能则 SQL（强白名单、易审计），不能（需要多步聚合、跨源印证、归并逻辑）则 Python（注册为可信代码）。

4 个 SQL 实现为 `quarter_end_integer_deposits`、`integer_transfer_aggregates`、`co_located_pairs`、`time_window_collision`。它们在 `functions.json` 中以 `impl:"sql"` 声明，并携带 `sql` 模板。SQL 实现受双重白名单约束：首先 `_FORBIDDEN_SQL` 正则（`core/functions.py:24`）命中 `INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/ATTACH/DETACH/COPY/TRUNCATE/GRANT/REVOKE` 即拒；其次 `_assert_readonly()`（`core/functions.py:30`）要求语句首词必须是 `SELECT` 或 `WITH`。参数经 `render_sql_template()` 渲染为类型化字面量，占位符 `{{param}}` 与 `parameters` 必须一一对应——多声明或少使用都硬失败（`core/functions.py:112-121`），从根上消除参数注入。

7 个 Python 实现为 `call_frequency_spike`、`call_pair_coverage`、`jian_cross_level`、`tipoff_cross_reference`、`org_interest_links`、`overpass_two_hop`，以及 `location_colocated`（实现独立在 `core/geo.py`，仅在本模块末尾注册进 `FUNCTION_IMPLS`，`core/functions.py:668-670`）。Python 实现走注册机制：`FUNCTION_IMPLS` 字典由 `@register_function(name)` 装饰器填充（`core/functions.py:139`）；`FunctionExecutor.invoke()` 通过 `inspect.signature` 兼容新旧签名——新签名 `fn(store, params, ctx)` 接收 `RuntimeContext`（可 `ctx.table()`/`ctx.link()` 取只读表名），旧签名 `fn(store, params)` 向后兼容（`core/functions.py:586`）。所有 Python 实现通过 `ReadOnlyStore` 代理访问数据（`core/functions.py:548`），且调用前由 `PolicyEngine` 对 `inputs` 中每个 `obj_*`/`lnk_*` 做 fail-closed 策略检查（`core/functions.py:609`）。

这套两类实现的划分对可审计性有实质意义。SQL 实现从声明到执行全程可静态审阅：`functions.json` 里的 `sql` 模板是"白纸黑字"，配合 `_FORBIDDEN_SQL` 与首词校验，审计者不必运行即可确认它只读、不拼接外部文本。Python 实现则相反——逻辑写在代码里，无法靠模板静态保证安全，因此用更重的护栏兜底：一是 `FUNCTION_IMPLS` 注册表把"可用函数"收敛为白名单，装载期校验 `functions.json` 的 `impl_ref` 存在（未知即硬失败，杜绝声明了一个不存在的实现），二是 `ReadOnlyStore` 在对象层屏蔽 `execute`/`conn` 等写入口（`core/runtime_context.py`），即便实现代码误写写操作也会在代理层被挡下。两条轨共同的出口是 `invoke()` 返回的字典：SQL 实现塞 `rows`、Python 实现塞 `result`，这一路径差异由 `_evaluate_single` 统一消化（`core/rules.py:37-49`）——也是 REQ-G-022 修复的历史坑：原先只读 `out["rows"]` 导致 R5 这类 `result` 路径的 py 实现永不匹配。

## 5.4 只读红线与防注入

Function 层是写路径之外的只读计算域，其红线目标是：任何声明的计算都**无法**修改数据、无法逃逸到未声明的语义表、无法注入任意文本。约束由三层叠加。

第一层是 SQL 关键字黑名单与首词校验，已在 5.3 说明（`_FORBIDDEN_SQL` + `_assert_readonly`）。第二层是 `{{param}}` 双向核对：模板里出现的占位符集合与 `parameters` 声明集合必须相等，缺声明或多声明都直接抛异常（`core/functions.py:114-121`）。这一设计意味着"模板少写一个参数"或"声明里多一个无用参数"都会在执行前失败，而不是把错位参数静默带入查询。第三层是参数值本身的类型与白名单校验，集中在 `check_param_value()`（`core/functions.py:75`）：`integer` 拒绝 `bool`（因 Python 中 `bool` 是 `int` 子类，必须显式排掉）、`decimal` 接受 int/float 同样排除 bool、`boolean` 严格用 `isinstance` 判定、`date` 必须是 `YYYY-MM-DD` 正则匹配；最关键的是 `string` 类型**必须声明 `enum` 白名单**——没有白名单或取值不在白名单内一律拒绝，自由文本不接受（`core/functions.py:90-96`）。例如 `quarter_end_integer_deposits` 的 `cash_summary_tokens` 与 `time_window_collision` 的 `exclude_org_suffix` 都只能取 `["现金存入"]`、`["公司"]` 之类枚举，从声明侧消除把任意字符串拼接进 SQL 的可能。

此外还有结构降级护栏：当 py/sql 函数抛 `CatalogException`/`BinderException` 且异常信息引用了 `obj_*`/`lnk_*` 语义层（`_is_structural_degrade`，`core/functions.py:47`），说明数据源未接入或 schema 不符，此时返回零命中并标 `degraded` 留痕（`core/functions.py:641-657`），而非让单条规则异常拖垮整条管线。这是"失败要留痕、不许静默"原则在计算层的落地。

逐条对照这些护栏，可以看清它们各自封堵的具体攻击面与出错模式。`_FORBIDDEN_SQL` 挡的是"声明的 Function 偷偷写了数据"——例如某分析师想用 Function 顺手建临时表，正则直接拒绝，因为 Function 不是写路径（`core/action_executor.py` 才是唯一写入口）。首词 `SELECT`/`WITH` 校验则挡住更隐蔽的写法，比如以注释或 CTE 名义夹带 `DELETE` 的变体。`{{param}}` 双向核对挡的是"参数漂移"——模板改了占位符却忘了更新 `parameters`，或反过来，两种都会让渲染出的 SQL 语义错位。`string` 必须 enum 这一条是 SQL 注入的最后防线：即便有人绕过模板直传参数，`check_param_value` 仍强制取值落在白名单，自由文本（如含单引号的恶意串）一律进不来。而 `integer` 拒绝 `bool` 是为防 Python 语义陷阱——`True` 在 DuckDB 里会被当成 `1`，若阈值参数被误传布尔会悄然改变判据量级。这些约束共同构成"声明即受信、但受信不等于不受限"的边界。

## 5.5 阈值策略与自适应

阈值是检测规则的灵敏度旋钮，其策略对象由 `core/threshold.py`（REQ-027）承载。核心入口 `resolve_rule_params(store, rule_id, params, pack)` 根据 `thresholds.json` 的声明，把 `rule.params` 副本中 `bound_param` 指向的阈值替换为计算值，并返回 `(params_copy, method, threshold_value, is_degraded)`。计算保持只读，绝不写回 `rule.params` 原值（`core/threshold.py:5-10`）。

它支持两种 method：`absolute`（绝对阈值）与 `relative_median`（分位数自适应）。`relative_median` 用于 R3 这类"相对常态"判据——从 `obj_call` 拉取频次样本，样本数 ≥ `min_samples`（默认 20）时取中位数 × `multiplier`（默认 2.0）作为阈值，并向上取整为整数次数（`core/threshold.py:139-163`）。这把"突发"定义为相对自身常态的倍数，比硬编码常数更抗数据分布漂移。但自适应必须戴两道枷锁：其一是 `bounded_by` 夹紧（`_bounded`，`core/threshold.py:79`），无论自适应算出的阈值多高多低，都被限制在 `thresholds.json` 声明的 `[min, max]` 区间内，防止极端样本把判据推到荒谬位置；其二是样本不足时**绝不硬算**——样本数 < `min_samples` 时回退到 `fallback` 值（或无声明时回落 30）并设 `is_degraded=true`（`core/threshold.py:164-171`），显式告知"本次阈值是退化的，结论需谨慎"。无论哪条路径，`is_degraded` 都会随 finding 透传给产物（`core/rules.py:207`），与 R3 函数自身的绝对阈值降级共同构成双重可审计标记。

自适应阈值的工程实现还带一个常被忽视的约束：确定性。`resolve_rule_params` 在 `relative_median` 分支内固定使用 `seed=20260501` 的 `random.Random`（`core/threshold.py:143-145`），即便当前样本路径不依赖随机，也显式消费一次随机数以保证"同一份输入 → 同一份阈值"在固定 seed 下可复现（REQ-027 AC4）。这与全书"确定性优先于智能"的原则一致——自适应不是引入不确定性，而是在确定性框架内选取阈值。另一个值得说明的现状是：`_collect_samples`（`core/threshold.py:90`）目前只对 R3 实现了样本拉取逻辑，其余规则尚返回空列表（注释明确"以后扩展时可按 rule→sample SQL 映射"）；这意味着除 R3 外，阈值目前仍走 `absolute` 或 `absolute_hardcoded` 路径，自适应能力是按需启用的而非全局默认。设计上这留了扩展点，但当前并不假装"所有规则都已自适应"。

## 5.6 零命中四分类

调查系统最危险的失败，不是漏报，而是把"数据根本没接进来"静默呈现为"没有异常"。`core/rules.py:120` 的 `_classify_zero()` 与 REQ-G-002 把零命中拆成四个语义不同的类别，目的是强制区分"没查"与"查了没中"。

- `data_absent`（info）：数据本来就没有——语义表缺失、列不符导致结构降级，或输入表 0 行。这不是"干净"，是"没数据"。
- `config_missing`（warning）：配置或知识包为空导致函数无法计算（如 R5 无 `case_knowledge.json`）。
- `empty_result_suspect`（warning）：数据齐全却零命中，**可疑**——可能匹配逻辑失效，需要人工确认。
- `clean_scan`（info）：确属干净。**必须由分析师在 `rules.json` 显式声明 `zero_is_clean`**（当前六条规则均未声明），系统不许自行把零命中解释为"排除"。

分类逻辑（`core/rules.py:120-138`）的优先级值得细读：若函数已标 `degraded` 则归 `data_absent`（结构降级已由 Function 层记 warning）；其次若函数自报 `config_missing` 则归 `config_missing`；若扫描到的输入语义表总行数 `scan_rows==0` 则归 `data_absent`；只有当有输入、未声明 clean、且函数正常返回空时，才归 `empty_result_suspect`。`scan_rows` 由 `_scan_rows()`（`core/rules.py:102`）统计 Function 声明消费的 `obj_*`/`lnk_*` 总行数，表不存在计 0。

这一步的关键价值在于：一条规则零命中时，系统不再裸 `continue`，而是向 `run_diagnostic` 落一条带 `zero_type` 的诊断对象（`core/rules.py:186-191`），且诊断与 findings **物理分离**（原则四）。于是审计者能立刻区分——"R4 零命中是因为 `lnk_co_located` 表压根没编译"（data_absent，属接入问题）与"R4 编译了但确实没有异人同地记录"（empty_result_suspect，属可疑需复核）。前者若被误读为后者，会让调查在错误的安全感中错过关键缺口。

最后一类 `clean_scan` 的"显式声明"要求是这套分类里最容易被挑战、却最必须坚持的一环。设计上我们选择 fail-closed：系统**绝不**自行把零命中解释成"排除了嫌疑"，`zero_is_clean` 必须由分析师在 `rules.json` 里写 `true` 才生效（`core/rules.py:136`）。理由是"没查到"在侦查语境下永远首先是"证据缺口"而非"清白证明"，让机器默认下排除结论，既越过了第三条禁令的边界，也会在复核时误导后人。因此当前 `default` 包六条规则无一声明 `zero_is_clean`，它们任何一条零命中都会落为 `empty_result_suspect`（warning）或 `data_absent`（info），把"是否需要补数据/查检测器是否失效"的决策显式留给人类。这四类标签与 `run_health` 的诊断种类（`reference-keypoints.md` 第 10 节）一一对应，最终汇入 `run_diagnostic` 与 findings 物理分离的统一出口。

## 5.7 互斥组与重叠消解

当两条规则本质刻画同一类现象、只是口径互补时，同时命中会产生冗余甚至矛盾的线索。`core/rules.py:52` 的 `_suppress_overlaps()`（REQ-025）负责在末段消解这类重叠。机制是 `exclusive_group` 声明：R1 与 R2 同属 `integer_amount` 组，但 R1 `primary_rule=true`、R2 `primary_rule=false`，且两者 `overlap_resolution="drop_if_primary_hit"`。

执行流程分三步：先收集所有 `exclusive_group` 中已被 primary 命中的组号集合；再逐条检查 finding——若其所在组已被 primary 命中、自身非 primary、且策略为 `drop_if_primary_hit`，则把它移入 `suppressed_log` 并从主列表移除，同时在该 finding 上挂 `suppressed_reason` 与指向 primary 的 `suppressed_by_rule`；最后把完整 `suppressed_log` 附到每条保留的 finding 上（`core/rules.py:52-99`），保证审计链完整、产物可还原"谁抑制了谁"。语义上，现金存入（R1）与整数对公转账（R2）都围绕"整数金额"，但前者聚焦个人现金走账、后者聚焦第三方过桥，组内以 R1 为优先、R2 仅在 R1 未命中时才进入主列表，避免同一笔整数资金被两条规则重复标记为两类候选。这是声明式消解而非代码分支，新增互斥关系只需在 `rules.json` 配 `exclusive_group`/`primary_rule`/`overlap_resolution` 三字段。

一个容易被误读的细节是：被抑制的 R2 并非被彻底丢弃。`_suppress_overlaps` 把它的 `suppressed_log` 条目（含 `suppressed_by_rule`、`reason`、原 `候选虚处`/`source_rows`）挂到保留的 finding 上（`core/rules.py:80-98`），实现"主列表干净、但消解关系可审计还原"。这是 REQ-025 的核心要求——重叠消解不能让信息消失，只能让"主视图"不重复，审计者随时能从 `suppressed_log` 看到"R2 当时也命中、只是被 R1 压下了"。当前 `overlap_resolution` 只实现了 `drop_if_primary_hit` 一种策略；若未来需要"保留低优先级但降级标注"等更细粒度消解，扩展点仍在 `rules.json` 的该字段而非 Python 分支。

## 5.8 庙算：五间 × 五维覆盖模型

"庙算"是侦查研判前的沙盘推演，对应代码 `core/hypotheses.py` 的 `MiaoSuan(pack)`。它的职责不是下结论，而是把虚实扫描产出的 findings 映射为候选假设，并量化"覆盖是否完整、对抗痕迹是否遗漏"——这是反遗漏（不漏掉该查的方向）的工程抓手。

五间（间类）由 `jians.json` 声明式定义：`因间`(org,bid_project,weight 3)、`内间`(tipoff,weight 5)、`反间`(transaction,weight 2)、`死间`(osint_article,org,weight 4)、`生间`(call,trackpoint,weight 1)（`ontology/default/jians.json`）。注意 `source_object_types` 决定哪些语义对象属于该间类，**名称可从声明增删，但间类的语义归属由声明驱动而非硬编码**。五维（维度）由 `dimensions.json` 定义：资金(transaction)、通讯(call)、行为(trackpoint)、关系(person/org)、时间(transaction/call/trackpoint)。

五间模型最关键的工程概念是**交叉升格**（cross-level escalation）：单源证据只是"观察"，两个独立源印证升格为"线索"，三个及以上源升格为"可立案依据候选"（`core/functions.py:360-362`、`core/lineage.py:157`）。其映射 `min_independent_sources` 为 1/2/3 这一关系**硬编码不可配置**，只有等级名称（观察/线索/可立案依据候选）可从 `cross_levels` 配置（`jians.json` 的红线注释与 `reference-keypoints.md` 第 7 节均强调）。这样设计的用意是：升格逻辑是方法论常量，不应因配置漂移而改变；而名称可本地化。更重要的是，即便达到"三源"，系统也只标"可立案依据候选"——机器绝不置"已立案"，与第三条禁令（不下定性结论）严格一致。

`MiaoSuan` 的覆盖模型提供四类可量化检查：`dimension_coverage()` 双轨口径——`declared`（假设声明了哪些维度，理论覆盖）与 `empirical`（扫描实际命中的维度，实证覆盖），且两者独立报警（REQ-G-009/024），因为"假设写了维度但扫描无命中"属"想到了没查到"，与"压根没想到"是不同补救动作；`jian_coverage()` 检查每个间类是否被假设引用，缺则警告"对抗痕迹未覆盖"；`conflict_check()` 检测两条假设是否引用同一笔 `source_rows`（提示需合并或区分）；`enumerate_space()` 用笛卡尔积展开枚举候补池，命中 `ENUM_BEHAVIOR_MAP` 的有检测器支撑可转正，其余入 `backlog` 待人工注入，且枚举空间**永不闭合**（传自定义 space 即可扩维）。假设本身受 `MAX_HYPOTHESES=5` 上限与"知己栏强制非空"约束（`core/hypotheses.py:163-165`），确保沙盘可控、可追溯。

把庙算放回五技能（skill）流水线能更清楚它的位置：技能链是 `miaosuan`（庙算）→ `zhi_ji_zhi_bi`（知己）→ `xu_shi`（虚实）→ `qi_zheng`（奇正）→ `yong_jian`（用间）（`skills/registry_bootstrap.py:245`）。庙算处于最前，负责"开打前先想清楚覆盖哪几个方向、漏了哪个维度、假设之间有没有抢同一笔证据"。`jian_cross_level` 这个 Function（`core/functions.py:332`）则在案件级独立计算五间交叉等级——它遍历 `obj_*`/`lnk_*` 中声明了 `jian` 的表，非空即算"该间类有数据源命中"，再用 `count_independent()`（`core/functions.py:276`，按 `source_independence.related_pairs` 并查集合并同源）算出独立源数，最终落到 1/2/3 的硬编码映射。需要强调交叉升格的**保守性**：单源只是"观察"、双源才"线索"、三源也仅是"可立案依据候选"——它把"证据的强度"显式分级，但始终保持"机器只提示、不置定性结论"的边界。未建模的数据源在 `jian_cross_level` 里会被诚实标为"缺口"而非充数（`core/functions.py:350`），这也呼应了零命中四分类"不得用缺口冒充干净"的同一纪律。

## 5.9 血缘去重与优先级排序

多条技能（虚实/Q2 过桥/Q3 通话等）可能从不同角度产出指向同一事实的线索，全量堆给正兵会造成信息过载。`core/lineage.py` 基于"血缘"做去重合并与优先级排序，使工作台只看最值得查的线索。

去重依据是血缘三要素（`core/lineage.py:9-17`）：① 数据溯源重叠度——两条线索 `source_rows` 的 Jaccard 相似度 ≥ `threshold`（默认 0.5）；② 间类归属——合并后 `jian_types` 取并集；③ 假设链——同 `assumption_chain` 推导属同一逻辑链。`dedupe_and_merge(clues, threshold=0.5)`（`core/lineage.py:52`）用并查集把同源线索归组，每组取字段并集、拼接 title、保留 `merge_log` 记录"由哪些原始线索合成"，返回数量 ≤ 输入。合并后若 `jian_types` 增多，会触发 5.8 所述的交叉升格——这正是"多源印证升格"在血缘层的落地：单源线索合并不再是单源，可能从"观察"升为"线索"。

优先级排序由 `prioritize_clues()`（`core/lineage.py:210`）完成，按 `confidence×间类覆盖×数据强度` 三维打分降序：置信度取假设链中最高者（默认 H1=0.9、其余=0.7），间类覆盖取命中间类最大权重 / 归一化基数，数据强度取 `min(1.0, 溯源行数/10)`。权重**从 `scoring.json`/`jians.json` 声明读取**（R7，不再硬编码），每条线索被打上 `priority_score`/`score_basis`/`cross_level` 供操作台展示。这样既让"高密级内间 + 多源印证 + 数据扎实"的线索排在最前，又保证排序逻辑可声明、可解释、可审计。至此，从判据声明、执行驱动、只读计算、阈值自适应、零命中留痕、重叠消解、庙算覆盖到血缘去重，整条检测链路在"确定性优先、失败留痕、写路径唯一"的约束下闭环。

去重阈值 `threshold=0.5` 是整个合并的灵敏旋钮：它要求两条线索的溯源行 Jaccard 相似度达一半以上才并组，过低会把"只是恰好引用了同一张表不同行"的线索误合并，过高则留不下去重效果。工程上它是 `dedupe_and_merge` 的入参（`core/lineage.py:52`），调用方（`run_all.py` 主线）传 0.5，未硬编进函数体，留了调参空间。`same_assumption` 与 `jian_intersect` 的"同假设链且间类有交集"作为补充合并条件（`core/lineage.py:93-95`），则兜住了"溯源行不完全重叠但逻辑同链"的情形——例如 R3（生间）与 R4（生间）都指向"二人私下关系"，即便引用的是通话表与轨迹表不同行，仍应合并升格。最后，线索处置状态通过 `save_statuses`/`load_statuses`（`core/lineage.py:310/344`）以 `INSERT ... ON CONFLICT DO UPDATE` 幂等落 L2 DuckDB，使正兵跨会话仍能恢复上次进度，审计链则保留在内存 `LineageClue.audit_log` 而非状态表，避免处置状态与审计职责混淆。
