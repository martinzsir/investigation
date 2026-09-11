# 第 4 章　数据接入与治理

数据接入与治理是语义层"之前"的一段。在原始多源异构数据（银行流水、通话记录、招投标档案、工商登记、轨迹出行、公开 OSINT、举报材料）进入冷层、再被编译成语义层之前，系统必须先回答四个问题：数据以什么格式进来、进来后是否可信、脏值如何被一致地清洗、以及如何把一张新表安全地接进来。本章即描述这一段的实现机制与工程取舍。值得强调的是，这一段的所有模块都遵循"只读观测、不写回断言、不替代人工判断"的底线——适配器只做格式归一，校验只做隔离不毁坏数据，检测器只落诊断不阻断管线，草案只写 `output/drafts/` 绝不触碰 `ontology/`。这种克制不是功能缺口，而是确定性内核对"机器只做计算与提示、绝不做定性结论"这一核心命题的工程兑现。

## 4.1 五格式适配器与统一 schema

数据接入的第一道关口是格式归一。项目面对的原始文件形态高度发散：银行导出的 CSV、财务系统的 Excel、第三方接口的 JSON、历史归档的 SQLite、以及分析人员早已落地的 Parquet。若让上游每个消费者各自解析这些格式，不仅重复劳动，更会在列名识别、类型推断上产生不一致的口径偏差。`data_ingest.py` 用适配器模式（Adapter Pattern）收敛这一问题：每种格式一个独立适配器类，由统一的 `DataIngestManager`（`data_ingest.py:197`）按文件扩展名分派。

适配器的注册表集中在 `ADAPTERS` 字典（`data_ingest.py:188`），键为扩展名，值为适配器类：`.csv/.tsv` 走 `CSVAdapter`、`.xlsx/.xls` 走 `ExcelAdapter`、`.json` 走 `JSONAdapter`、`.sqlite/.db` 走 `SQLiteAdapter`、`.parquet` 走 `ParquetAdapter`。新增一种格式只需新增一个适配器类并登记进 `ADAPTERS`，主流程 `ingest_directory`（`data_ingest.py:254`）与 `ingest_file`（`data_ingest.py:221`）无需改动——这是"开闭原则"在本项目里少数被明确兑现的地方之一。

五个适配器的职责是单方向的：只读原始文件、做最小必要的列名映射与类型推断，然后产出统一的内部标准 schema。字段映射表 `_COLUMN_MAP`（`data_ingest.py:34`）把"交易日期/date/时间""交易金额/amount/数目""付款方/姓名/name"等常见列名归一到 `{主体, 对方, 金额, 日期, 用途, 来源文件, 来源sheet, 原始行}` 这一套标准列。设计上刻意保留了"原始行"（raw 列），这是后续血缘溯源 `core/row_uri.py` 的物理基础——即便归一出错，原始证据仍在。

这里有一个明确的工程取舍：适配器对待"缺列"采取"容错优先"策略。`_validate`（`data_ingest.py:281`）对银行流水要求 `主体/金额/日期`、通话记录要求 `主体/对端`、中标档案要求 `项目/公司`，但若缺失仅打印警告并保留接入（`data_ingest.py:291`），不中断流程。理由是"正兵（默认角色）可能只上传部分列"，下游语义层会据此降级而非崩溃。与之配套的是类型强制时的"只降级不静默"：金额列含逗号/货币符号会被剥离为数值（`_coerce_types`，`data_ingest.py:51`），无法解析的日期被 coerce 为 NaT，但损失计数记下 `df.attrs["coerce_lost"]` 随接入记录上报。这呼应了设计原则第 4 条"失败要留痕，不许静默"。

值得注意的是，适配器的类型推断与 4.6 节画像脚本的"raw 只读"是两套互不相让的路径：前者为接入效率做隐式转换，后者为接入前画像刻意 `dtype=str` 不做任何转换（`scripts/profile_table.py:45`），以免金额/日期被 coerce 后值类型识别失效。两条路径的并存不是冗余，而是"跑通管线"与"评估数据"两个诉求的分离。

## 4.2 分区校验与隔离区

格式归一之后、语义层消费之前，每个数据分区（partition）还要经过一道独立的校验闸门，实现于 `core/ingest_validate.py`（REQ-005）。校验的核心抽象是三个数据结构：`IngestPartition`（`core/ingest_validate.py:21`）记录分区元信息（dataset、high_watermark、schema 指纹、行数、内容哈希）；`ValidationError`（`core/ingest_validate.py:33`）携带错误码、分区、详情与严重级别（block/warn）；`QuarantineResult`（`core/ingest_validate.py:42`）汇总隔离结论。

校验维度被收敛为四类，它们覆盖了原始数据最常见的结构性缺陷：

- `DATA_GAP`（数据缺口）：分区表不存在、不可读，或行数为 0。这是最硬的失败，默认 `severity="block"`（`core/ingest_validate.py:79`）。
- `PK_DUPLICATE`（主键重复）：主键列去重后剩余行占比超过阈值（默认 1%，`pk_dup_threshold=0.01`，`core/ingest_validate.py:55`）时报警，防止重复记录污染后续去重与加权。
- `SCHEMA_DRIFT`（schema 漂移）：实际列集与期望列集（expected_columns）出现并集差异即报错（`core/ingest_validate.py:91`），用于捕捉上游在不知不觉中改了导出结构。
- `TIME_NON_MONOTONIC`（时间非单调）：对日期类列检查是否存在倒退（`core/ingest_validate.py:115`），这是交易流水被错切、乱序合并的早期信号。

这四类校验码是被显式枚举的有限集合，而非开放的字符串——这意味着校验结果的下游消费（诊断、报表）可以按固定维度聚合，而不必面对不可预期的自由文本。`validate`（`core/ingest_validate.py:52`）逐分区执行上述四项，任何 `severity="block"` 的错误触发隔离动作。`severity` 字段的 block/warn 二分也服务于分级响应：block 类（表缺失、schema 漂移、主键超阈、时间倒退）必须隔离，warn 类（如空分区的 `DATA_GAP` 默认 warn，`core/ingest_validate.py:74`）则仅提示、不入隔离——这是"宁可误放提示、不可误糟数据"在严重级别上的细化。

隔离区（quarantine）的设计意图是"阻断但不毁坏"。不合格分区不会被删除，而是写入专用表 `partition_quarantined`（`core/ingest_validate.py:174`），通过 `quarantine`（`core/ingest_validate.py:184`）落账，并通过 `is_quarantined`（`core/ingest_validate.py:205`）供下游查询。被隔离的分区从主消费路径退出，但仍可经由诊断回看"为什么被剔"。这是一种典型的 fail-closed 思维在存储层的落地：宁可让分区退出计算、也不让带病数据偷偷进入语义层。同时要注意，隔离与第 3 章的"脏值降级"是两层不同的处置：脏值降级在语义层 CAST 阶段逐行修复或剔除个别字段，而隔离区是针对整个分区的结构性拒收，二者不重叠。

## 4.3 清洗 op 注册表

跨过隔离区的数据在编译进语义层之前，还要经过清洗（clean）/转换（transform）处理。`core/clean_ops.py`（REQ-D-004）是这一能力的唯一注册表（single source of truth）。强调"唯一"是因为清洗规则一旦散落各处，同一字段在不同表里可能被洗成不同结果，进而破坏确定性内核最看重的"同一输入必得同一输出"。把全部 op 收敛到一个注册表，配合 loader 的 fail-closed 校验（binding 引用的 op 必须已注册，否则装载期硬失败），就从结构上杜绝了规则漂移。

注册表的对外接口有四个核心函数。`register_op`（`core/clean_ops.py:35`）注册一个 op，结构非法或同名重复注册都会硬失败（REQ-D-004 AC-2，`core/clean_ops.py:59`）；`validate_op`（`core/clean_ops.py:77`）在指定层（clean/transform）校验 op 是否可用，未知 op 或层不匹配直接抛错；`compile_sql_expr`（`core/clean_ops.py:129`）把声明的 op 链编译为 SQL 表达式，注入到语义层编译的投影内；`build_clean_context`（`core/clean_ops.py:213`）按 `clean_rules.json`（REQ-D-007）构造清洗上下文，支持 merge/replace 两种词表合并语义。

每个 op 以 `OpSpec`（`core/clean_ops.py:21`）描述，关键字段是 `impl`（py 或 sql）、`layer`（clean/transform/any）、以及可选的 `sql_template` 与 `param_enum`。`layer="any"` 的 op 在 Python 侧与 SQL 投影内均可使用（如 `strip_thousands/strip_currency/cn_date_norm`），因为它们同时提供了 `fn` 与 `sql_template` 双实现；而 `reject_if`、`trim_prefix` 等带参 op 仅注册为 clean 层，是因为它们的参数（自由文本/正则/换算因子）在 Python 侧处理无 SQL 注入风险，但若进入 SQL 投影层则强制要求 `param_enum` 白名单（`core/clean_ops.py:103`），红线不放宽。

当前已注册的 15 个 op 分别是：`strip_thousands`（千分位逗号剥离）、`strip_currency`（货币符号剥离）、`cn_date_norm`（中文日期归一）、`digits_only`（仅留数字）、`strip_cc`（剥手机国家码）、`strip_paren`（剥括号注释）、`despace`（去空白）、`to_upper`、`to_lower`（大小写归一）、`pad_date`（日期补零）、`reject_if`（条件拒行，param=contains_mask）、`trim_prefix`/`trim_suffix`（去前缀/后缀）、`regex_extract`（正则提取）、`unit_convert`（量纲换算）。这套集合覆盖了侦查数据最常见的脏格式，且不依赖任何第三方库，与离线部署目标一致。

需要特别点出与第 3 章的衔接：清洗层不是独立跑一遍再交给语义层，而是在语义层编译时注入。源字段的 transform op 链经 `compile_sql_expr` 编译为 SQL 表达式，注入位置位于 `CAST` **之前**——即先 `regexp_replace` 把"￥1,280.50"洗成"1280.50"，再 `TRY_CAST` 成 DOUBLE。这一注入点决定了脏值降级第 1 步（CAST 失败）能拿到已经规整过的字符串，而不是原始脏值。第 3 章已展开脏值降级三态细节，本章仅在此确认衔接边界：op 注册表是清洗能力的唯一出处，而 op 链的实际生效在语义层编译之内。

## 4.4 数据元标准与合规扫描

清洗解决"值长什么样能被解析"，数据元（data element）解决"值应该长什么样"——类型、长度、格式、校验位、敏感度、单位。这是两类不同层级的约束：`clean_ops` 是过程能力，`data_elements` 是验收标准。`core/data_elements.py`（REQ-D-001）是数据元标准注册表，声明落在 `ontology/<pack>/data_elements.json`，平台级共享定义放 `_shared/data_elements.json`（含 `DE_IDCARD/DE_PHONE/DE_AMOUNT/DE_DATE`，以及 `idcard_mod11`、`luhn` 两个校验算法）。

校验算法本身也是注册式的。`CHECKSUM_ALGOS`（`core/data_elements.py:12`）是算法名到校验函数的字典，`register_checksum` 同名重复注册硬失败。`checksum_idcard_mod11`（`core/data_elements.py:26`）实现 GB 11643-1999 公民身份号码校验位（ISO 7064:1983 MOD 11-2），按 17 位加权求和取模 11 对照校验码；`checksum_luhn`（`core/data_elements.py:38`）实现 Luhn 算法（ISO/IEC 7812-1），用于银行卡号/IMEI 等。被声明 `checksum` 的数据元在合规扫描时自动调用对应算法——未知算法在装载期硬失败（fail-closed，REQ-D-001 AC-2），不降级放行。

合规扫描由 `core/compliance.py`（REQ-D-016）执行，签名同检测器 `scan(gateway, ...)`。它只消费已引用数据元（`objects.json` 属性声明 `{"data_element": "DE_X"}`）且已物化的属性，按 format/checksum/range/enum 四类检查逐项比对，检查项可单独启停（调用方 `checksum` 参数或 `data_elements.json` 顶层 `compliance_checks` 声明，AC-6）。违规行不以异常中断管线，而是以"对象.属性 + 代理键 + 违规码"落 `run_diagnostic`（`kind=compliance_violation`，`core/compliance.py:162`），违规样本一律经 `_mask_sample` 脱敏。四类违规码分别是 `format_mismatch`、`checksum_failed`、`range_violation`、`enum_unknown`（`core/compliance.py:34`）。

这里有一个清晰的边界：合规扫描是"对官方标准对账"，它只覆盖"已声明数据元的属性"，未引用数据元的列根本不在其视野内（AC-8）。这意味着数据元的覆盖度本身就是治理成熟度的一部分——没有被数据元约束的列，虽然还能进语义层，但失去了机器可读的验收门。这也正是 4.6 节数据元驱动推荐（REQ-D-021）存在的价值：让新表的列尽量挂上数据元，把"野列"收敛到"受约束的列"。

## 4.5 三类启发式质量扫描

数据元合规是"硬标准对账"，而还有一类问题无法用硬标准覆盖，只能用启发式去嗅探：某列实际是敏感列但没在 `policies.json` 声明遮蔽、某金额列的单位其实和其他表不一致、某张表的流水已经是半年前的旧数据。这类问题由三个同签名 `scan(gateway, ...)` 的检测器承担，分别是 `core/sensitive_scan.py`（REQ-D-018）、`core/unit_scan.py`（REQ-D-020）、`core/data_freshness.py`（REQ-D-019）。

`sensitive_scan` 走两路启发：列名词根（`COLUMN_HINTS`，如 id_card/手机/卡号，`core/sensitive_scan.py:29`）与值模式（18 位身份证、11 位手机、16–19 位银行卡，按优先级首中即停、身份证先于泛卡号以控误报，`core/sensitive_scan.py:36`）。值模式命中率需 ≥ `hit_ratio`（默认 0.3，`core/sensitive_scan.py:64`）才报，避免偶发命中被放大。已在 `policies.json` 声明遮蔽的属性不再报（去重口径，AC-2）。

`unit_scan` 聚焦金额类数据元（decimal/integer）。当同一数据元被多个对象引用、各列中位数跨对象相差 ≥ `ratio_threshold`（默认 10000 倍，正好是元与万元的典型量级差，`core/unit_scan.py:44`）时，提示疑似单位混用或金额突增；金额类属性未声明 `unit` 时给 info 级提示（AC-3）。`data_freshness` 按对象 date 属性取 `MAX(数据时间)` 与当前日期比对，超 `stale_days`（默认 180 天，`core/data_freshness.py:41`）告警；关键是空时间属性直接跳过、绝不误报为"很旧"（`core/data_freshness.py:67`），且它回答"数据本身多久没更新"，与本体版本新鲜度（FRESH/STALE）分开显示、互不混淆。

这三类扫描的统一设计哲学是：**只告警不阻断**。在 `run_all.py` 主数据流里，它们位于语义层编译之后的"质量门"位置（`reference-keypoints.md` 第 11 节），全部落 `run_diagnostic`，返回诊断而不抛异常。为什么不能升级为阻断？因为启发式天然伴随误报：一个账号列里偶有 11 位数字不一定就是手机号，金额突增可能是真实的大额交易而非单位混用。把误报变成阻断，等于让噪声拥有否决权，会严重伤害系统的可用性——分析师将频繁被假阳性卡住，久而久之要么绕过、要么无视所有警报。但反过来，"只告警"又带来另一种风险：使用者可能把告警当成机器兜底、误当权威结论。因此三个检测器在输出里反复强调"只提示不定性""需人工核对"，且下游消费（如画像质量分）对纯启发式扣分标记为"可人工推翻"（`REVIEWABLE_DEDUCTIONS`，`core/ontology_profile.py:225`）。告警是留给人的判断入口，不是机器的判定终章——这正契合核心命题"机器只做计算与提示，绝不做定性结论"。

## 4.6 列画像与本体草案

接入一张全新外部表时，系统需要先把表"看清楚"，再决定怎么接。这段能力由值类型识别、六层画像、草案组装三块构成，核心约束是"只观察、只建议、不写回"。

值类型识别模块 `core/value_type.py` 是画像的地基。它用两类判定：否定式（否定即排除歧义的可靠信号）`phone/id_card/date_str/amount/account/number`，按特异性降序构成 `ORDER`（`core/value_type.py:33`），顺序即判定优先级——特异性强的排前，避免账号正则吞掉手机号/身份证；肯定式（需确认）`person/org`，由机构名启发词兜底。`analyze_column`（`core/value_type.py:89`）输出类型分布、混装标记（同一列出现 ≥2 个归一落点即 `mixed`）与落点建议。混装是这一模块要解决的真实痛点：当 `transaction.from_raw` 里账号与人名混写、`org.raw_name` 里机构名与人名混写时，归一到哪一类不明确，选错就是链断裂。

六层画像在 `core/ontology_profile.py`，由 `OntologyProfiler`（`core/ontology_profile.py:235`）编排，覆盖 L1/L2/L3/L4/L5（L0 见 `core/data_map.py`，见 REQ-P M4 注释 `core/ontology_profile.py:210`）。它消费已物化语义层，逐对象给出空值率、基数、混装、合规违规率、变体数、间类分布等维度，并产出可被人工资推翻的启发式扣分（如肯定式识别、变体）。需要强调的是，画像与 4.5 的启发式扫描共享"告警非判定"的立场：画像只观察、不写回任何库表，结论一律标"待核实"。

新表接入的画像入口是 CLI `scripts/profile_table.py`。它刻意以 raw 模式（`dtype=str`、不做任何类型转换，`scripts/profile_table.py:45`）读外部表，再调 `build_table_profile` 做列画像与候选关联（外部列 distinct 与已物化 `obj_*` 可连接属性的 overlap ≥ 阈值）。随后 `DraftAssembler`（`core/draft_assembler.py`）把画像升级为"诊断 + 生成"：组装出 `objects/links/bindings` 三件草案，每张草案头带 `_draft/_status=待核实/_evidence`，证据链可追溯到具体列与 overlap 比。

`DraftAssembler` 有一条被源码扫描固化的红线：全部输出**只写 `output/drafts/<table>/`，绝不写 `ontology/`**（`core/draft_assembler.py:277` 的 `write_drafts`）。其配套函数 `recommend_steps`（`core/draft_assembler.py:303`）零 IO 副作用，只按依赖排序输出 ETL 步骤清单（混装拆分先于绑定、清洗规则先于冷层建表、绑定后必须 reprofile 复检）。这条红线的意义在于：机器生成的草案再像样，也只是"待核实"候选；人工审核复制进 `ontology/<pack>/` 后，还需经 `build_ontology` 的 loader 校验两道闸才生效。草案与生效声明之间的人为闸门，正是"声明是数据、实现是代码"这一设计原则在新表接入场景的具体兑现。

## 4.7 新数据源接入的六步路径

把前述各模块串起来，新数据源接入形成一条固定六步路径（见 `reference-keypoints.md` 第 16 节），其最大特色是：**全程不改任何检测器的 Python 代码**。这并非偶然便利，而是"声明是数据、实现是代码"与"写路径唯一/检测不改"等设计目标的直接兑现。

第一步，`profile_table.py` 对列做画像与值类型识别，产出列画像、候选关联、以及 4.4 提到的数据元驱动推荐（`core/de_recommend.py`，REQ-D-021）。推荐器有两条置信阈值：`CONFIRM_THRESHOLD=0.70`（`core/de_recommend.py:25`，命中率低于此值不推荐）与 `HIGH_THRESHOLD=0.90`（`core/de_recommend.py:26`，达此值且为强信号才标 high，否则 medium 需人工确认）。它永不自动写 `objects.json`、低置信一律标 `needs_confirmation`、混装复合列只给拆分提示——红线与 DraftAssembler 同源。

第二步，`DraftAssembler` 生成 `objects/links/bindings` 三份草案，只落 `output/drafts/`，绝不碰 `ontology/`。第三步，分析师审阅草案，确认对象名、pk、name_property、候选关联后，手工复制到 `ontology/<pack>/`。第四步，若新表需要新计算能力，在 `functions.json` 声明（Python 实现还需注册 `FUNCTION_IMPLS`），并在 `rules.json` 写 `rule_text` 与参数。第五步，在 `policies.json` 补策略声明——这一步极易被遗漏，但遗漏会在运行时被 fail-closed 拒绝（未声明即拒绝，呼应设计原则第 5 条），所以必须显式补齐。第六步，`validate_ontology.py --strict` 做严格校验，通过后 `build_ontology.py` 编译生效。

六步路径的精妙处在于它的"封闭性"：检测器（compliance/sensitive_scan/unit_scan/data_freshness）的能力边界由数据元声明、属性遮蔽声明、值模式启发式决定，而这些都是声明式配置。接入一张新表时，分析师改的是 `ontology/` 下的 JSON 与 `clean_rules.json` 词表，而非去改 `core/compliance.py` 的正则或 `core/sensitive_scan.py` 的 `COLUMN_HINTS`。当一份新数据需要新校验逻辑时，正确做法是把它表达为新的数据元声明（挂 format/checksum/range）或新的属性遮蔽策略，而不是 fork 检测器。这种"能力通过声明扩展、实现保持稳定"的纪律，保证了内核在持续接入新数据来源时，不会因为检测器各自膨胀而丧失整体一致性与可审计性——这也是第 8 章"已知缺陷与技术债"中需要持续守护的边界之一。从 `reqd_case` 观察，即便接入已经过精心构造的脏数据路径（缺对端列的 `call_old`、整列降级的 `cp_whole`、拆分路径的 `cp_split`），只要声明层面的列映射与降级策略写清，编译期与运行期就能给出确定且可回放的处置，而不必回头改一行检测器代码。这条路径的可重复性，正是六步接入设计价值的最终体现。
