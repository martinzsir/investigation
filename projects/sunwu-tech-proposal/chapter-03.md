# 第 3 章　语义层：Ontology 编译内核

第 1 章已经把"语义层"、"对象（obj_*）"、"链接（lnk_*）"、"代理键"、"降级"等核心术语与三条禁令、六条设计原则交代清楚，第 2 章则给出总体架构与 ADR-1/ADR-2/ADR-3（引入语义层、声明式本体、类型与管道分离）。本章不再论证"为什么要有语义层"，而是把镜头对准编译器本身，回答一个更具体的问题：**这套 obj_*/lnk_* 语义表，究竟是怎么从一个目录下的 JSON 声明被编译出来的**。语义层不是数据库里的静态表，而是一段可重复执行、可回滚、可增量重建的确定性计算过程，其全部"查什么、数据从哪来、怎么判定、谁能看"都写在 `ontology/<pack>/*.json` 里，Python 只提供编译器（`core/ontology.py`）、装载器（`core/ontology_loader.py`）与执行器，不承载任何业务 SQL。理解编译内核，是理解后续规则引擎、写路径、权限与增量重建的前提。

## 3.1 为什么需要语义层

多源异构数据进入侦查内核时，最大的隐性成本不是"把数据读进来"，而是"让每一种消费方都能正确读懂每一种数据"。在孙武侦查官的真实案件包里，原始数据天然横跨七类来源：银行流水、通话记录、招投标档案、工商信息、轨迹出行、公开 OSINT、举报材料（这七类也正是三条禁令中 `_FORBIDDEN_TABLES` 所列的直查拦截对象）。而语义层在 `default` 包中要向上提供 11 类对象（person、org、account、transaction、call、trackpoint、bid_project、clue、decision、tipoff、osint_article）。设想没有语义层、由规则与检测器直接读原始表：每一条规则（R1–R6）为了计算"资金""通讯""关系"等概念，都必须各自理解这七类源表的列约定、编码习惯与口径差异，这就构成了典型的 N×M 问题——七个源模式乘以十一类对象语义，理论上需要维护 77 组"源表→概念"的映射，且每新增一类数据源，所有消费方都要被改一遍。

语义层把这个交叉积拆成了两段单向依赖：第一段是"源表→对象"的一次性绑定，由 `bindings.json` 声明、编译器执行，只做 N 次；第二段是"对象→规则"的消费，规则只说 obj_*/lnk_* 的词汇，完全不接触原始列名，只做 M 次。N×M 因此被压成 N+M。一个具体的对照是：R4「二人公示期轨迹同框」在 `rules.json` 里只写"直接 SELECT `lnk_co_located`（不同主体同地点 ±1 天）"，它完全不必知道轨迹出行原始表叫什么、经纬度列叫什么；银行流水、工商信息等各自如何变形进 `obj_transaction` / `obj_org`，是 bindings 一次性解决的问题。若去掉语义层，这条规则就得自己解析轨迹源表的 schema，七个源模式就乘以六条规则，维护成本与出错概率同步放大。除此之外，语义层还顺带解决了三个没有它就无法低成本解决的问题：其一是统一类型系统（原始 CSV 里的"金额"可能是字符串，进入 obj_transaction 必须落为 DOUBLE，见 3.6）；其二是统一代理键（原始表没有稳定主键，语义层用内容哈希生成，见 3.5）；其三是 fail-closed 的授权边界（未声明即拒绝，见 3.2）。换句话说，语义层本质上是把"数据对齐"这件最容易出错、最容易被写成散落各处的临时胶水代码的事，收口成一个可审计、可重放的编译步骤。这正是 ADR-1 引入语义层、ADR-2 采用声明式本体的根本动机。

## 3.2 八段声明模型（schema_version = 2）

语义层的全部"是什么"与"从哪来"由一组 JSON 声明描述，这些声明被划分为八个带 `schema_version = 2` 标记的段落，各自承担单一职责。类型层只回答"是什么"：`objects.json` 声明对象的主键（pk）、类型（entity 或 event）、身份/展示属性（name_property）与属性→值类型的映射，**刻意不包含任何数据来源信息**；`links.json` 只表达对象之间的关系与边属性，**只表达关系、不含任何检测判据**。管道层回答"数据从哪来"：`bindings.json` 通过 object_bindings（source / source_sql / clean / optional）与 link_bindings（build_sql）把类型层挂到具体数据源上。规则手册 `rules.json` 用自然语言 `rule_text` 写判据，并挂唯一的机器执行钩子（function / params）；能力目录 `functions.json` 给出这把钩子的实现，SQL 实现强制 SELECT/WITH 白名单、参数用 `{{param}}` 占位，Python 实现需注册进 `FUNCTION_IMPLS`。写路径 `actions.json` 声明角色、必填参数、副作用与终态，其 `allowed_from` 由 `states.json` **反向派生**而非手写。语义视图 `views.json` 按角色投影、查询时派生不物化；权限面 `policies.json` 给出对象级/链接级策略与属性级敏感列遮蔽。

八段声明的分层不是文档约定，而是被装载器强制的硬契约。装载器 `load_pack` 在读取 `objects.json` 前就要求 `schema_version == 2`，一旦不符立即抛 `ValueError` 使装载期硬失败，不存在任何灰度兼容。这套分层同时贯彻了"类型层与管道层分离"的设计原则：`objects.json` 里找不到 source 字段，`bindings.json` 里找不到判据字段，两者通过对象名在装载期拼接，任何越界声明都会在后续对账中被发现。

与八段声明平行存在的，是一组**非八段、不被编译进 obj_*/lnk_* 的配置/知识文件**。它们同样以 JSON 存放于 `ontology/<pack>/`，但不参与语义表物化，而是喂给其它子系统：`thresholds.json`（阈值策略）、`dimensions.json`（五维声明）、`jians.json`（五间声明）、`states.json`（状态机）、`enum_space.json`（枚举空间）、`scoring.json`（评分）、`derived_properties.json`（派生属性）、`data_elements.json`（数据元标准）、`llm_policy.json`（LLM 治理）、`clean_rules.json`（案件级清洗词表）。其中尤其值得强调 `case_knowledge.json`——它是**人名唯一合法存放处**，检测器与分析逻辑一律不得把自然人姓名硬编码进代码（CI 静态扫描 `scan_hardcoded_names.py` 兜底），破案主体身份只能源于这份声明。把"知识"与"声明"分开，是为了让编译内核保持稳定：新增一个枚举值、补一份评分配置，都不应触发语义表的重建或破坏八段 schema 的校验。

权限面的 fail-closed 是八段模型不可绕过的强制点。`policies.json` 中未声明的对象或链接，运行时一律拒绝访问；文件本身缺失则 `_missing_file=True` 导致全拒。这与第 1 章"未声明即拒绝"的设计原则一致，也意味着接入新数据源的第六步必须补 `policies.json`（见参考基线第 16 节），漏写不会静默放行，而是被运行时拦死。

## 3.3 装载流程与硬依赖顺序

声明文件只是静态输入，真正进入编译前要先被 `load_pack`（`core/ontology_loader.py:102`）组装成一个内存中的 `OntologyPack` 对象。这一步不是简单地按文件名顺序读取，而是一条有严格硬依赖的 13 步装载链，顺序背后的依赖关系决定了某些文件必须先于另一些文件读取：数据元标准 `load_data_elements` 必须先于对象装载，因为 `objects.json` 里属性的 `data_element` 引用需要校验该 ID 已在数据元注册表登记（`data_elements.json` 缺失则回落 `_shared` 包基线）；敏感属性遮蔽集 `_load_property_mask_set` 必须先于对象，确保敏感 data_element 已声明遮蔽；五间声明 `load_jians` 必须先于对象与链接，因为对象/链接上的 `jian` 字段必须已在 `jians.json` 声明；状态机 `load_states` 必须先于 `actions.json`，因为 action 的 `target_status` 必须落在已声明状态集内；维度声明 `load_dimensions` 必须先于 `rules.json`，因为规则的 `dimension` 必须是已声明维度（缺省回落内置五维）。其余依次是 `_load_objects` → `_load_links` → `_load_bindings` → `_load_actions` → `_load_functions` → `_load_rules` → `load_enum_space` → `_load_clean_rules`，最终组装 `OntologyPack` 并写入缓存。

这 13 步的硬性顺序不是性能优化，而是正确性约束：若 `objects.json` 先于 `jians.json` 装载，装载器无法校验 `jian` 字段合法性，只能把错误推迟到编译或运行时才暴露，那便违背了"装载期尽量早失败"的工程取向。整个链条的单点失败都会向上抛异常，不存在"缺一段也能凑合跑"的降级路径——这正是对声明式本体可信度的保障。

装载器还引入了一层**指纹缓存**以避免重复解析全部 JSON。入口处 `_pack_fingerprint` 计算声明目录的指纹（基于文件 mtime/内容），与 `pack`、`base_dir` 共同构成 `cache_key`，命中 `_PACK_CACHE`（`core/ontology_loader.py:44`，模块级字典）则直接返回缓存的 `OntologyPack`，跳过整条 13 步解析。缓存失效的判据就是指纹变化：任何一个声明文件被修改，指纹随之改变，下一次 `load_pack` 必然重新装载。这一机制对增量重建尤为关键——同一进程内反复调用 `build_ontology` 时，声明未变则复用缓存对象，声明变了则强制刷新，从而保证"编译结果"与"声明内容"严格对应，杜绝了陈旧缓存导致的诡异不一致。需要注意的是，缓存只加速"声明→OntologyPack"的解析阶段，不影响"OntologyPack→obj_*/lnk_*"的物化阶段；物化阶段的正确性由版本时钟（3.7）独立保障，二者职责分离，互不掩盖错误。

另外一个常被忽略的细节是：指纹缓存是**进程内**的模块级字典，不跨进程、不落盘。这意味着如果两个独立进程（例如 Web 服务与 CLI 脚本）同时运行，它们各自维护各自的缓存，不会因共享内存而产生竞态；代价是同一份声明在两个进程里会被各解析一次。对于离线单进程的侦查内核而言，这种取舍是合理且安全的——它把"缓存一致性"这个难题降级为"单进程内幂等"，而非引入跨进程锁。

## 3.4 编译流程

装载得到 `OntologyPack` 后，`build_ontology`（`core/ontology.py:393`）执行真正的物化。它的整体流程可分成六个阶段，对外返回 `{"objects": {...行数}, "links": {...行数}, "skipped": [...], ...}` 的统计字典。

第一阶段在事务之外准备上下文：`load_pack` 拿到声明后，先算 `_default_org_names` 并调用 `build_clean_context` 汇成清洗上下文（`clean_rules.json` 合并/替换内置基线词表，对应 REQ-D-007），再 `_load_entity_mapping` 读入 REQ-016 受保护的归并映射。第二阶段进入 `BEGIN TRANSACTION` 的原子块：逐个非 runtime 对象执行 `_compute_object_rows` → `_create_obj_table` → `_insert_object_rows`；随后逐个非 runtime 链接执行 `CREATE TABLE lnk_<name> AS <build_sql>`。链接物化后有一次**列对账**——编译器取出 `lnk_*` 的实际输出列集合，与 `links.json` 声明的边属性逐一比对，任何声明属性不在输出列中即抛 `ValueError` 硬失败（见 `core/ontology.py:448-455`，注释明确写"不准带病编译"）。整段 Object+Link 物化要么全部成功 `COMMIT`，要么在异常时 `ROLLBACK`，保证语义层不会出现"对象建好但链接残缺"的中间态。

第三阶段 `ensure_runtime_tables`（`core/ontology.py:496`）按类型声明为 runtime 对象/链接建空表（`CREATE IF NOT EXISTS`，不碰既有数据），典型如 `obj_decision`、`lnk_decision_for`——它们不由编译器填数据，只在 ActionExecutor 副作用写路径时被插入。第四阶段 `ViewMaterializer.materialize_all(skip_missing_base=True)` 物化 Object Views（REQ-046）；`skip_missing_base=True` 使得若某视图的基对象因 optional 而未建表，则跳过该视图而非拖垮整次 BUILD，与链接编译失败跳过的口径一致。第五阶段 `compute_version` + `record_version`（`core/ontology_version.py`，REQ-001）为本次构建写入一条 `is_current=true` 的版本记录，构成版本时钟。第六阶段 `snapshot_source_rows`（`core/row_uri.py`，REQ-008）把源行做内容寻址归档，分区标记为 `BOOTSTRAP_PARTITION`（全量启动，不继承旧索引），使任何一行 obj_* 都能回溯到原始明细行。这里值得停下来理解事务边界的取舍：Object 与 Link 的物化被包在单一事务里，但 runtime 表、Views、版本时钟、溯源归档都发生在 `COMMIT` 之后。这不是疏漏，而是刻意的层次划分——原子性要保障的是"语义表本身要么全成、要么全无"，而版本时钟与溯源是"本次成功构建的副产品"，它们建立在已提交的一致快照之上。下游的统一读网关 `OntologyReadGateway`（REQ-002）读取的永远是某个已 `COMMIT` 的完整语义层，不会看到半成品；版本时钟与溯源归档即使在本阶段之后出错，也只影响可观测性，不污染语义表本体。这种"核心物化强原子、周边产物弱依赖"的分层，使编译内核在异常时既不会留下脏表，又不会因为溯源写失败而把整个 BUILD 判废。

关于 runtime 表还需点明一点：它们虽不承载编译数据，却是写路径的"落点"。`obj_decision` / `lnk_decision_for` 由 `ActionExecutor._create_decision` 在分析师做出 `file`（固证/立案）类副作用时插入，编译器只负责"把空表先建好、把 schema 锁定"。这保证了无论写路径何时触发，目标表一定存在且列序稳定——把"建表"与"写数据"分离，也意味着 runtime 表的 schema 受语义层版本管理，而非散落在 ActionExecutor 的临时 DDL 里。

## 3.5 代理键的幂等分配

这是本章最关键、也最容易被低估的机制。语义层能否"反复重建而主键不漂移"，直接取决于代理键（surrogate key）的分配方式——而这一点决定了历史线索、审计链、跨案引用能否始终对得上行。

设想一种最朴素的方案：按插入顺序给每个实体编 `person_0001`、`person_0002`……这种方案在首次构建时没问题，但一旦源数据新增一个名字并重新构建，所有后续序号都会整体后移一格，`person_0005` 可能从"张三"变成"李四"。语义层之上沉淀的每一条线索、每一次审计记录、每一处外键引用，都会因此指向错误的主体。对侦查系统而言这是灾难性的：历史研判结论与当下数据发生静默错位，且完全无迹可寻。孙武侦查官因此采用**内容哈希式代理键**：键只由内容决定，与插入顺序、与其它名字的存在与否都无关。

代理键的分配由 `_compute_object_rows`（`core/ontology.py:978`）在清洗、归并、业务键去重**之后**统一调度，具体落在 `ontology.py:1095-1114` 的分发逻辑，键的哈希实现由 `_proxy_keys`（`ontology.py:354`）与 `_event_proxy_keys`（`ontology.py:365`）两个助手完成。它按对象类型分三条策略：

其一是 **event 型**（transaction / call / trackpoint 等过程性事件）。事件没有稳定"名字"，编译器把每一行内容排序后取 `sha1(json.dumps(行内容))[:12]`，前缀 `prefix = otype.pk.split("_")[0]`（如 `txn_`、`call_`）。内容完全相同的多行（如同一天同一金额的两笔）按确定性顺序追加 `_02`/`_03` 后缀保证逐行唯一。这里有一处精细处理：`exclude_idx` 会排除 composite（复合）属性列的索引（REQ-D-013 AC-5），使得"仅备注/拆分标记不同"的两行仍被视为同一事件，不会因复合列差异而裂变出不同主键。

其二是 **entity 型**（person / org / account 等主体）。它按 `name_property` 的值排序，对每一个名字算出 `{name: f"{prefix}_{sha1(name)[:12]}"}`。这是严格幂等的：一个名字永远映射到同一个键，后续新增任何别的名字都不会改变既有名字的键。这正是 REQ-004 增量重建"只重写受影响行"的前提——若键随插入顺序漂移，增量更新就不可能只改写局部。

其三是 **自引用对象**（pk 等于 name_property，例如 clue）。它直接直通源自然键，不做重映射，因为 runtime 链接要按这个自然键关联。

需要特别强调分配时机：代理键是在"实体归并映射（REQ-016，variant→canonical 折叠）"与"业务键去重（REQ-D-015）"**之后**才生成的。归并映射把分析师确认过的别名变体折叠到同一 canonical，去重按业务键剔除重复导入，二者都发生在键分配之前，因此合并后的变体行会共享同一个代理键，不会出现"同一个真实的人有两个主键"的分裂。这一点对侦查内核尤为致命：如果"张卫国"与"老张"被识别为同一主体却拿到两个代理键，那么分别挂在两个键下的资金线索、通话线索将无法在图谱层汇合，五间交叉升格（第 7 章）会漏算来源数，直接动摇"可立案依据候选"的判定。幂等键因此不只是一个工程整洁度问题，而是多源线索能否正确汇聚的前提。

`prefix` 的取法同样有讲究：`prefix = otype.pk.split("_")[0]`，即取主键名第一段。例如 `obj_person` 的 pk 是 `person_id`，前缀就是 `person_`，生成的键形如 `person_a1b2c3d4e5f6`。这种前缀让代理键在肉眼可读的同时保持命名空间隔离——`person_` 与 `org_` 永远不会撞键，排查数据时一眼能看出键属于哪类对象。它与审计哈希链（第 7 章 core/audit.py 的 SHA-256 链）形成呼应：审计链保证"操作不可篡改"，幂等键保证"被指向的主体稳定不变"，二者共同让"某条审计记录指向某人"这个事实在反复重建后依然成立。这条链路环环相扣：幂等键 → 增量只改受影响行 → 历史外键不漂移 → 审计链与线索可回放。没有它，语义层就只能"构建一次、不可重建"，而"可反复重建"恰恰是孙武侦查官把侦查变成确定性计算过程的根基。

## 3.6 类型系统与脏值降级

语义层面对的原始数据天然脏、乱、缺。编译内核的设计取向非常明确：**脏值可以降级，但 BUILD 绝不中断**。这意味着任何单行的类型错误、缺列、坏值，都不应导致整张 obj_* 表编译失败，而是被分类、留痕、降级处理，与 findings 物理分离地进入诊断（对应设计原则"失败要留痕，不许静默"）。

类型系统的核心是 `TYPE_SQL` 映射，把声明里的值类型翻译成 DuckDB 列类型：string→VARCHAR、integer→BIGINT、decimal→DOUBLE、date→DATE、boolean→BOOLEAN、timestamp→TIMESTAMP、duration_days→INTEGER、enum→VARCHAR、json→VARCHAR。`objects.json` 里每个属性都标注值类型，编译器据此建表与转换。

转换阶段对坏值采用 `TRY_CAST`，其处理存在**三态选择**，由 binding 的 `on_cast_error` 与对象属性配置决定：默认态是"降级为 NULL"，并把 `obj_x.prop<-raw: N 行不可转 t` 这样的计数记进统计，供诊断回溯，BUILD 继续；当某属性显式声明 `on_cast_error=fail` 时，转换失败升级为硬失败，整次构建中止——用于那些"类型错了就毫无意义"的关键列；第三种态是 `quarantine`（隔离），整行被剔除并落入 `build_quarantine` 表，同时追加隐藏列 `__raw_<alias>` 保留原始值用于事后判定，既不污染主表、也不丢失证据。`reqd_case` 包里的 `cp_whole`（composite 整列降级）、`call_old`（缺对端列降级）正是这条路径的验证样本。

对于"缺列"这一特殊脏值，编译器区分两种预检结果：`raw=None` 即源表根本无该列时，渲染为 `CAST(NULL AS TYPE)`，保持 obj_* 的列集与列序稳定，不破坏下游 schema；而列级预检在编译 SQL 前先用 `DESCRIBE` 检查源表——声明为必填的列缺失则**硬失败**，且报错直指缺失列名（不再裸抛 `BinderException`）；声明在 `optional_columns` 里的可选列缺失则降级为类型化 NULL 并留痕（`source_column_missing`）。

三态选择在实践中并非随意配置，而是按"该列对判定有多关键"逐属性决定。金额、日期这类驱动规则计算的核心列，通常用 `on_cast_error=fail`——转不出来说明源数据根本不可用，宁可中止也不产出似是而非的零值；姓名、备注等展示性或辅助性列用默认降级 NULL，保证主表不空；而那些"整行都不可信"的场景（如关键复合字段整体损毁）才动用 `quarantine`，把整行隔离保留而非丢弃。这种分级让编译器在"宽松到丢数据"与"严格到一碰就挂"两个极端之间取得了可声明、可审计的中间态，也呼应了参考基线中"降级但不静默、留痕但不污染"的统一降级协议（REQ-G 系列）。此外，清洗 op 链经 `compile_sql_expr()` 注入，且位于 CAST **之前**——先清洗、后转型，避免脏格式直接进入类型转换。

多源场景另有专门的裁剪逻辑。当一个对象由手写多源 `UNION`（如 `source_sql` 跨六类源表聚合 `obj_person`）声明时，若只导入了其中部分源表，编译器通过 `_prune_union_sql()` 按"已挂载的源表"剔除引用缺失表的分支后再编译，避免 `CatalogException` 拖垮整次 BUILD；全部分支都缺失时才按"源表缺失"同口径跳过。这条路径把"部分接入"变成了受控降级，而不是编译失败。

## 3.7 版本时钟与增量重建

语义层不是一次性产物，它需要随源数据更新而演进，但不应每次都全量重算。支撑这一点的，是 `core/ontology_version.py`（REQ-001）实现的版本时钟与 `core/rebuild_planner.py` 规划的增量影响面。

版本时钟的核心是**双 watermark**，它们各自回答一个不同的问题。源端 watermark 取自"含日期/date/time 列的源表"的 `MAX` 时间，回答"源数据是不是已经变了"；语义层 watermark 记录上一次成功构建的时间点，回答"语义层是不是还没跟上源端"。两者比较得到状态：源端前进而语义层落后 → `STALE` 并附带 `affected_objects` 清单；从未构建过 → `UNBUILT`。把两个问题分开，是为了让调度方精准判断"该不该重建"以及"重建哪些对象"，而不是笼统地全量重跑。

除了时间 watermark，编译器还维护两份内容哈希以支撑指纹级判断：`_compute_input_hashes()` 对每个对象按 pk 排序后取全行 JSON 的 sha256，反映"输入数据变了没"；`_compute_params_hash()` 则把规则参数、函数参数、**以及 views 声明**一并哈希，反映"声明/参数变了没"。任一哈希变化都触发相关对象的重建。把 views 声明也纳入参数哈希是一个容易被忽略但很重要的细节：视图投影属于"语义层对外呈现的契约"，视图声明变了意味着下游读到的列子集变了，理应触发重建而非沿用旧版本时钟——否则版本号前进了，视图契约却没更新，会出现"版本说已最新、实际投影已过期"的错位。

双 watermark 与双哈希合起来回答四个独立问题：源端时间是否前进（源 watermark）、语义层是否落后（层 watermark）、输入数据是否变化（输入哈希）、声明参数是否变化（参数哈希）。调度方据此区分"全量重建"与"局部增量"，避免在数据纹丝未动时做无用重算，也避免在声明微调后误判为"无需重建"。这种把"变没变"拆成可独立观测的多个维度的做法，是增量重建既快又不出错的基础。

实际的增量执行由 `materialize_changed(conn, plan)` 完成，其顺序是**对象先于链接**（链接依赖对象键），过程为：先把受影响对象物化进 TEMP 的 `_STAGE` 临时表，再用 `_diff_apply()` 做行级 diff（只把增/删/改的行应用到正式 obj_*），随后推进版本时钟、执行 `snapshot_source_rows` 溯源归档，并发出 `ontology.materialized` 事件通知下游。行级 diff 的粒度意味着一次增量只动变化的行，这又回扣到 3.5 的幂等键——正因为键由内容决定，diff 才能稳定地按主键对齐新旧行。

增量重建有一类特殊保护对象：`entity_mapping`（REQ-016 受保护归并映射）不被重建覆盖，避免归并结果被冲掉。批处理规模由 `rebuild_planner.py` 的 `RebuildPlan` 与 `DEFAULT_BATCH_THRESHOLD=5000` 控制，超过阈值的增量任务会被分批，防止单次事务过大。

## 3.8 Object Views 与 DerivedProperty

语义层对外暴露的不只有物化的 obj_*/lnk_*，还有两类"查询时派生、不物化"的投影：Object Views 与 DerivedProperty。它们的共同设计哲学是——能算出来的，就不存下来，从而避免物化数据与派生数据之间的漂移。

Object Views 由 `core/views.py`（REQ-046）实现，声明在 `views.json`，被 `ViewMaterializer` 编译为 DuckDB 的 `v_<name>` 视图。视图本质是 `SELECT <属性子集> FROM obj_<base_object>` 的 SQL 投影，只引用基对象的列子集，**不复制数据、不引入新数据来源**。标准视图还会自动生成：每个对象一套 `v_<obj>_basic`（pk + name_property）与 `v_<obj>_full`（全部属性），均全角色可见，细粒度权限仍交给 `PolicyEngine` 在读时执行，不在视图层重复声明。这一点的安全含义很重：视图只是投影，**权限事实仍按 base_object 判定**——读视图时仍经 `PolicyEngine.check_object` 与 `apply_row_masks`（对象级 + 属性级遮蔽），视图本身不持有任何"谁能看什么"的事实。更进一步，`v_*` 不进入 `Store.query` 的安全表白名单，任何绕过 `OntologyReadGateway.view()` 直查 `v_*` 的行为都会被视作越权嫌疑。视图定义本身也在装载期纳入 schema 校验，base_object 引用必须存在、properties 必须是对象属性子集、roles 必须落在 `ROLE_RANK` 内，任一不一致硬失败。

DerivedProperty 由 `core/derived.py`（REQ-028）实现，与 Object Views 同源思路：属性在查询时按需派生，而非预先物化进表。`_FORBIDDEN_REGISTRY_NAMES` 明确禁止把"评分（scoring）"之类动态计算结果塞进静态注册表——评分不是客观存在的事实，一旦物化就会与实时计算脱钩、产生错误的"既成事实"印象。把这类派生属性限定在查询时计算，既保证了结果随时与最新数据一致，也守住了"机器只做计算、绝不置定性结论"的边界。

两类机制合在一起，构成了语义层"物化稳定事实、派生动态视图"的双层结构：obj_*/lnk_* 承载经过类型校验与代理键对齐的硬事实，Views 与 DerivedProperty 承载面向角色、面向场景的软投影，二者在查询入口汇合于统一读网关，既隔离了底层脏数据，又不让派生层成为新的数据漂移源。

把 Object Views 与 DerivedProperty 放在同一节对照，是因为它们常被混淆，却有不同的适用边界。Object Views 解决的是"同一份对象、不同角色看不同列"的投影需求，本质是列的子集与角色过滤，挂在 `views.json` 声明、编译为数据库视图；DerivedProperty 解决的是"某属性本身就不是源数据、需要按公式算出来"的需求，例如一个对象的衍生风险分值，它不属于任何源表，也不应被当作既成事实物化。二者的共性是不物化、查询时现算、权限仍回退到 base_object；差异在于前者是"已有列的裁剪投影"、后者是"按需计算的新列"。把这两者从 obj_*/lnk_* 物化流程中剥离出来，还带来一个架构上的好处：调整一个角色的视图或一个新派生属性，不需要重跑整个语义层编译，只需刷新视图定义或派生函数，编译内核的"稳定事实"部分因此得以保持低频变更、高频查询的特性。
