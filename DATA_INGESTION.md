# DATA_INGESTION.md

数据接入操作手册：`data/*.parquet`（L3 冷层）→ `investigation.duckdb`（L2 温层）→ `obj_*/lnk_*`（语义层）。

> 口径日期 2026-09-29。配套：`AGENTS.md`（禁令与命令）、`INSTALL.md`（环境）。

## 1. 数据流与三条红线

```
人工/采集源表 parquet ──► init_duckdb.py（CTAS 业务表 + v_flow/v_calls 视图 + 预聚合）
物品原始三表 ──► prep_item_registry.py（py 预装配）──► 物品登记/物品持有.parquet ──┘
                                │
                                ▼
                    build_ontology.py 按 bindings.json 编译
                    obj_*（对象）+ lnk_*（链接）+ 自动 enrich_location
```

- 红线 1：检测器/图库/MCP **不直读 parquet**，一律消费 `obj_*`/`lnk_*`。
- 红线 2：换数据源/改列映射**只改 `bindings.json`**，不改检测器代码；源列别名必须是 objects.json 已声明属性。
- 红线 3：物品身份只落 sha256 摘要（R14），凭证明文不进任何产物。

## 2. data/ 根目录文件清单（15 个 parquet）

行数为当前演示数据快照。

### 2.1 原始业务源表（外部采集/人工登记，可替换）

| 文件 | 行 | 列（类型） | 接入对象 | binding 要点 |
|---|---|---|---|---|
| 银行流水.parquet | 29 | 日期 VARCHAR、主体、对方、金额 BIGINT | `account`+`transaction` → `lnk_transfers` | transform：`金额` strip_thousands+strip_currency、`日期` cn_date_norm+pad_date；`币种` 可选列 |
| 通话记录.parquet | 114 | 日期 VARCHAR、主体、对端、次数 BIGINT | `call`；`person` 的 UNION 源之一 | `日期` 可选列；transform 同上日期链 |
| 轨迹出行.parquet | 139 | 日期 TIMESTAMP_NS、主体、地点、时刻 TIMESTAMP_NS | `trackpoint` → 反向物化 `location` | `时刻` 可选列（缺列降级 NULL，**不得补 00:00:00**）；`时刻` transform cn_datetime_norm |
| 工商信息.parquet | 3 | 主体、法人、状态、关联 | `org` | 法人/状态/关联为可选列 |
| 招投标档案.parquet | 7 | 项目、中标公示日、中标方、分管领导 | `bid_project` → `lnk_time_window`/`lnk_involved_in` | 中标公示日走日期 transform；分管领导可选 |
| 举报材料.parquet | 3 | 举报日期、分类、被举报人、举报人、内容 | `tipoff`；`person` UNION 源（被举报人） | 整表 optional（缺失跳过） |
| 公开OSINT.parquet | 4 | 主体、公开信息、发布日期、来源 | `osint_article`；`person` UNION 源 | 整表 optional；采集时间/保留天数为可选列 |
| 人员信息.parquet | 4 | 姓名、证件类型、身份证号、手机号、性别、案件类别 | `person_identity`（数据元演示对象） | 整表 optional；身份证/手机 clean_rule 由数据元自动挂接 |

**注意**：`person` 无独立主档源表，由 通话记录/轨迹出行/公开OSINT/银行流水/举报材料 五表 UNION 主体名（`strip`+`exclude_org_tokens` 清洗）生成。新增任何一个源表都会自动扩充 person 集合。

### 2.2 物品域原始三表 → prep 预装配（勿直接接 bindings）

| 文件 | 行 | 列 | 说明 |
|---|---|---|---|
| 物品台账.parquet | 6 | 物品名称、物品类型(中文)、凭证号、所在地、特征描述、经度 DOUBLE、纬度 DOUBLE | 一件物品一行；物品类型限词表：车辆/房产/票据/药品批号/设备/涉案物品 |
| 物品持有登记.parquet | 13 | 物品名称、凭证号、持有人、开始日期、结束日期、登记序号 INT | 一次持有一行；日期含时刻的自动分流到时刻列（R5 缺失=NULL 无哨兵） |
| 物品轨迹.parquet | 11 | 物品名称、凭证号、日期、时刻、地点、经度、纬度 | **可缺**；独立表，绝不并入「轨迹出行」（那表主体是人） |

### 2.3 prep 产物宽表（`prep_item_registry.py` 生成，**勿手改**）

| 文件 | 行 | 消费方 |
|---|---|---|
| 物品登记.parquet | 6 | `obj_item`（标识=digest json、轨迹=json） |
| 物品持有.parquet | 13 | `obj_hold_record` → `lnk_holds`（无凭证明文列，关联键=(类型,标识摘要)） |

### 2.4 系统产物/缓存（勿手改）

| 文件 | 行 | 来源 |
|---|---|---|
| geocode_cache.parquet | 10 | 地理编码缓存（高德在线/人工标注），enrich_location 可选二次 JOIN；缺失降级不报错 |
| Q1_time_window.parquet | 7 | init_duckdb 物化的 Q1 时间窗碰撞明细（供奇兵复用） |

另：`data/gen_sim.py` / `data/gen_sim_item.py` 是演示数据生成器（重建本目录演示 parquet）；`data/reqd_case/`、`data/test/ingest/`（五格式夹具）、`data/ref/`（区划参考库）为子目录，不在本手册范围。

## 3. 常用操作

### 3.1 全量首装 / 重建（default 包，WSL）

```bash
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.prep_item_registry"  # 仅物品三表变更后需要；打印跳过原因（无凭证且无特征行）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.init_duckdb"      # 建业务表+视图+预聚合+Q1 产物
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python -m scripts.build_ontology"   # 编译 obj_*/lnk_*，自动 enrich_location
```

顺序要点：**prep 必须先于 init_duckdb**（init 会把 物品登记/物品持有 CTAS 进库）；`build_ontology` 末尾自动做地点富化（区划五级 + 坐标回填）。或一条命令全链路：`python run_all.py --auto-review --no-cli`。

### 3.2 替换/更新某个源表

1. 同名覆盖 data/ 下 parquet（列集保持一致，见 §4）；
2. 物品三表变更 → 先跑 prep；
3. 重跑 init_duckdb + build_ontology。

### 3.3 新增数据源（声明式，不改检测器）

1. parquet 放 `data/` 根（default 包）或 `data/<包名>/`（其他案件包）；
2. 新对象 → `ontology/<pack>/objects.json` 声明 pk/kind/properties 值类型；
3. `bindings.json` 加 object_binding：`source.table` + `columns{属性别名: 源列}`，按需配 `transform`（编译前抢救：strip_thousands/strip_currency/cn_date_norm/pad_date/cn_datetime_norm）、`clean`、`optional`（整表可缺）、`optional_columns`（列可缺）；
4. `policies.json` 同步声明对象策略——**漏声明 = 运行时 fail-closed 被拒**；
5. init_duckdb：parquet 存在则 CTAS 并补声明列，缺失则按声明建空表（AC1）；
6. build_ontology 物化；若需关系边，links.json + link_bindings.build_sql 表达关系（检测判据一律进 rules/function，不写进 build_sql）。

### 3.4 Web 五格式在线接入（W-010/012）

入口：侧栏「数据接入 → 接入向导」（`/c/wizard`，五步 Stepper）。前置：顶部案件选择器先选案件（接入按案件归属）。同组另有「数据画像」（`/c/profile`）与「接入建议」（`/c/suggest`）两页。

1. **上传文件**：点击上传区选文件，支持 CSV/TSV/Excel/Parquet/JSON(NDJSON)/SQLite。上传后自动生成指纹（文件名+SHA-256+行数）与列画像；SQLite 多表文件可选库内表。嵌套 JSON 未展平/分隔符不匹配会给「解析预警」但不阻断。
2. **列分析与映射**：选目标表（案件快照 bindings 声明的源表集合），系统给自动匹配建议（含置信度 %，低置信琥珀高亮）；缺必选列仅警告**不阻断**，可手动下拉调整或选「不导入此列」；每列显示样本值/空值率/推断类型；数据元推荐可点「采纳」（仅向导内记录，不自动回写 bindings）。下方质量预览面板汇总：空值率 >50% 警告、匹配置信度 <70% 提示、潜在复合列提示、数据元合规预检（格式/校验位/值域/枚举）。
3. **质量预览与处置（三分类）**：A 类无损归一（despace/strip_thousands/strip_currency/cn_date_norm 等）一键「应用」；B 类可能丢信息（digits_only/reject_if 等）先「预演」（影响行数+前后样本对照）再「确认创建草稿」；C 类仅建议。草稿在 state.sqlite，状态：草稿/已确认/已发布/已驳回。
4. **确认导入**：汇总文件/格式/行数/目标表/已映射列/指纹；注意采纳与草稿**本批次不写 bindings**（发布接线属后续批次）。点「确认导入」。
5. **完成**：立即返回任务 ID，后台执行 IMPORT（落 `cases/<cid>/cold/<表>.parquet` 原子 rename，失败零残留）→ clean 合并进快照 bindings → 链式 BUILD。跳「任务中心」看 SSE 实时进度。同指纹文件 409 拦截，显示「文件已存在，已跳过」，不重复入队。

权限：上传登录即可；**导入需偏将及以上（clearance≥2）**，不足时报「数据导入需偏将及以上权限」。

API 对照：`POST /cases/{cid}/sources/upload` → analyze → mapping → `POST .../sources/{uid}/import`（409=同指纹幂等拦截）；目标表名过白名单正则且须在快照 bindings 声明内。

### 3.5 Web 接入物品数据的口径（重要）

接入向导的目标表里**有**「物品登记」「物品持有」（bindings 声明的源表），**没有**物品台账/物品持有登记/物品轨迹——原始三表是 prep 输入，不进 bindings。

向导 IMPORT 管道**不跑** `prep_item_registry`（digest、中文类型映射、轨迹序列化都在 prep py 层）。因此：

- **原始中文三表不能直接走向导**：列对不上（缺 标识/标识摘要），且 `凭证号` 不是声明列会被映射校验拒绝——这正是 R14 护栏，明文凭进不了语义层。
- **推荐路径（批量）**：原始三表放 `data/` → prep → init → build（§3.1），Web 端只消费结果（研判画布物品层/持有链）。
- **向导路径（仅限已预处理文件）**：线下先跑 prep 产出 物品登记.parquet / 物品持有.parquet，再用向导上传到对应目标表（案件级补充场景）。上传文件必须遵守产物格式：`类型` 列是英文枚举代码（vehicle/realestate/…），`标识摘要` 是 sha256 hex，`标识`/`特征`/`轨迹` 是 json 串。
- **前置**：item/hold_record 的 binding 是 `optional:true`——案件无这两表时 BUILD 静默跳过，画布物品层显示「语义层无 obj_item 表（物品数据源未接入或未重建语义层）」。
- 界面登记入口（正兵填中文「车牌号」自动归一为 plate）是 `core/item.py normalize_identifier_kind` 的设计意图，**当前尚未接线到任何 Web 路由**。

### 3.6 季度增量分区（REQ-004/005/018）

命名 `{数据集}_{yyyy}Q{n}.parquet`（如 `银行流水_2024Q4.parquet`），跑 `python -m scripts.incremental --quarter 2024Q4`。校验四维度：分区存在性 / 主键重复率(>1% 拒) / schema 漂移 / 时间单调性；不合格**隔离**并退出码 2，分区文件缺失退出码 1（不静默跳过）；同分区同内容重复应用自动跳过。

### 3.7 地理编码（三轨）

① 区划离线（AdminMatcher + `data/ref/` 民政部 CSV，零网络，build 时自动）→ ② 高德在线门牌级（`python -m scripts.geocode_locations --online --amap-key <KEY>`，结果落 geocode_cache.parquet）→ ③ 人工标注（直接维护 geocode_cache.parquet，`source=manual`）。富化优先级：cache > admin_offline > raw_fallback（无坐标降级不报错）。坐标全线 GCJ-02，不转换。

## 4. 格式与质量护栏速查

- **金额**：允许千分位/货币符字符串，transform 在 SQL CAST 前抢救；reqd_case 演示 `on_cast_error: quarantine`（转换失败整列隔离）。
- **日期**：ISO / 中文日期均可（cn_date_norm+pad_date）；日期列缺失可降级 NULL（optional_columns）。
- **时刻**：纯日期落 date 列、含时刻落 timestamp 列；**无时刻留 NULL，禁补 00:00:00**（防伪装时刻级判据）。
- **脏值**：编译期 TRY_CAST 失败降级 NULL + `source_value_cast_failed` 诊断，不中断 build。
- **clean 规则**：strip / exclude_org_tokens / despace / digits_only / strip_cc / reject_if:contains_mask（reqd_case 演示带参 op）。
- **物品**：中文类型词表只增不改语义；无凭证且无特征的行 prep 跳过并打印原因（R-4）；别名/标签词表真源在 `ontology/_shared/data_elements.json` enum_meta（改词汇改 JSON，不改代码）。
- **视图路径**：v_flow/v_calls 用相对路径挂载（CWD=项目根），Windows/WSL 双侧可读。
- **验证**：改动后 `python run_tests.py`（WSL venv，140 组全绿）；接入口径相关组：`itemsource`、`deingest`、`ingest`、`ingestapi`、`ontology`。
