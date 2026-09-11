# 测试案例 demoD 体检报告

- 检查对象：`cases/demoD`（冷层 8 张源表 / 9 个上传批次 / v8.duckdb / state.sqlite / artifacts）
- 检查时间：2026-09-11
- 方法：全部结论来自库内数据与项目自身代码复算，**未改动原案件**（所有重量验证均在 `.workbuddy/` 下的副本库上进行）
- ontology 快照：`cases/demoD/ontology/default` 与主仓 `ontology/default` **18 个文件 md5 完全一致**，无快照漂移

## 一、结论速览

| # | 结论 | 严重度 |
|---|------|--------|
| 1 | `obj_person` 与 binding 声明不自洽：应为 27 人，实际只有 19 人，漏 8 人；连带 `lnk_owns` 少 8 条账户归属边 | 高 |
| 2 | `person` UNION 只取「银行流水.主体」、不取「对方」，与 `obj_account`（主体+对方双边）口径不一致 | 中 |
| 3 | 空名实体 `person_da39a3ee5e6b`（raw_name 为空串）承接了 7 条边，与夹具注释「被举报人 NULL（无身份，INNER 丢边）」期望相反 | 中 |
| 4 | 时间新鲜度不识别未来日期：`2099-01-01` 算出 `age=-26410` 天，被判「数据新鲜」，5 张表静默通过 | 中 |
| 5 | `audit_chain` 17 条记录的 `rule_version` / `function_version` 全为 NULL | 低 |
| 6 | `quality_check v8`：21 项检查 6 通过 / 3 告警 / 12 违规（数据元违规多为夹具脏值注入，属预期） | 低（需确认） |

未通过 ∧ 但 **不影响** 检测输出：规则命中在「当前陈旧态」与「重建后」完全一致（R1/R3/R4/R5/R6 各 1 条，R2 为 0）。

## 二、通过项（无需处理）

- **引用完整性**：14 组链接端点外键检查，孤儿边 **0**
- **溯源覆盖率**：10 张 obj_* 的 `source_rows` **100% 非空**；语义层无任何直读 Parquet 的痕迹
- **链接物化算式核对**：`lnk_transfers`(85)、`lnk_calls_to`(72)、`lnk_time_window`(44)、`lnk_osint_mentions`(6)、`lnk_tipoff_targets_person`(13)、`lnk_owns`(9) 与 `build_sql` 重算结果全部一致
- **版本链完整**：8 次导入 → 8 次构建（v1–v8），`audit_chain` 17 条 = 1 条建案 + 8 组 `source_imported` / `build_succeeded`，无断链
- **脏值降级留痕**：`build_stats_v8` 记录 10 项 TRY_CAST 降级（金额 9 行、日期 7 行等）、2 条 `exclude_org_tokens` 清洗剔除，全部可溯源，Build 未中断
- **合规边界**：9 条线索全部 `待查`；`obj_decision` / `lnk_decision_for` / `action_request` / `clue_disposal_status` / `review_decision` 均为 0，无越权写、无定性结论
- **重复上传无害**：`工商信息` 上传两次，两次内容 11 行完全同哈希，冷层仅保留一份

## 三、问题详述

### 1. obj_person 未随「银行流水」导入重算（高）

`bindings.json` 声明 person 为六路 UNION：

```
通话记录.主体 ∪ 通话记录.对端 ∪ 轨迹出行.主体 ∪ 公开OSINT.主体 ∪ 银行流水.主体 ∪ 举报材料.被举报人
```

按此定义在全部源表就位后应得 **27 人**，库内只有 **19 人**，缺失 8 人：

`华清越` `樊皓宁` `郁晨曦` `鲁以墨` `王𠀀` `测试😀表情` `李志强（宏业法人）` `张卫国␤(零宽空格)`

**版本时钟证据**（`meta_ontology_state.input_hashes`）：

| 版本 | 时间 | person hash |
|------|------|-------------|
| v6 | 17:48:49（通话记录导入后） | `b647072b44f2` 首次出现 |
| v7 | 17:49:05（银行流水导入后，account/transaction 新增） | `b647072b44f2` **未变** |
| v8 | 17:49:14（招投标导入后，当前版本） | `b647072b44f2` **未变** |

即 person 自 v6 起再未被重算；其余对象同理均为「导入当次定型」。

**复算验证**（在副本上，两种路径都一步修正）：

- `build_ontology(conn, pack="default")` → obj_person 19 → **27**，lnk_owns 9 → **17**
- `rebuild_from_partition(dataset="银行流水")` → 同样 19 → **27**，rewritten_rows=16
- 声明级依赖本身是对的：`objects_for_dataset(银行流水) = ['person','account','transaction']`

⇒ **不是编译器缺陷**（同样输入能算对），而是该案件最后一次有效构建没有把 person 纳入影响集。建议追查 demoD 末次 build 的触发路径（服务器 worker 走的是 `build_ontology`，但产出与全量重算不等价，说明中间有另一条未含 person 的路径被走到）。

业务影响：本次夹具未产生线索差异（第二节结论），但 8 个有资金往来的人没有实体，`lnk_owns` 缺 8 条归属边，涉及人员补全、关系网检索时会静默漏人。

### 2. person 取数口径与 account 不一致（中）

`obj_account` = 银行流水.主体 ∪ 对方（双边），而 `obj_person` 只取 `银行流水.主体`（单边）。
结果：**只出现在收款方一侧的人永远进不了 person**。典型如「戴若曦」在银行流水中有 14 行（均在对方列），重建后仍不在 obj_person，其账户也就永远不会有归属边（30 个账户、重建后仍只有 17 条 owns）。

建议二选一：把 `对方` 补进 person UNION（与 account 口径对齐），或在 binding 注释里明确这是有意边界。

### 3. 空名实体造成误链（中）

obj_person 中存在 `raw_name = ''` 的实体 `person_da39a3ee5e6b`，并被 7 条边当作真实端点：

| 链接 | 条数 | 样例 |
|------|------|------|
| lnk_calls_to.from_person | 1 | `'' → 葛天菲` |
| lnk_calls_to.to_person | 1 | `葛天菲 → ''` |
| lnk_tipoff_targets_person | 1 | 举报对象为空（夹具原注释：「被举报人 NULL（无身份，INNER 丢边）」） |
| lnk_tipoff_from_reporter | 2 | 举报人为空 |
| lnk_owns | 1 | 账户名为空 |
| lnk_osint_mentions | 1 | OSINT 主体为空 |

夹具注释明确期望「INNER 丢边」，实际却因为存在空名实体而成边。建议在 person 的 clean 链里加 `drop_blank`（或在 link build_sql 的 JOIN 前过滤空/gnu空白），让缺失主体回到「边不成立」。

### 4. 新鲜度不识别未来日期（中）

`core/data_freshness.py` 只有 `if age > stale_days` 一个判据。demoD 夹具特意注入三条 `2099-01-01` 未来记录（OSINT 文章 / 举报 / 轨迹），全部得到负数天数并被判为「数据新鲜」：

```
osint_article.pub_date  最新 2099-01-01，距今 -26410 天，数据新鲜   ← 未告警
tipoff.submit_date      最新 2099-01-01，距今 -26410 天，数据新鲜   ← 未告警
trackpoint.date         最新 2099-01-01，距今 -26410 天，数据新鲜   ← 未告警
trackpoint.date         最新 2021-10-01，距今 1806 天 > 180 天      ← 正确告警
```

未来时间戳通常是清洗/时区错误信号，建议增加 `age < 0 → data_freshness_future` 告警。

### 5. audit_chain 缺规则版本（低）

17 条审计的 `rule_version` / `function_version` 全为 NULL（build 类事件粒度尚可解释，但规则溯源时拿不到版本锚点）。

### 6. quality_check v8 明细（低，需人工确认）

- **数据元违规 10 项**：身份证 9/20（校验位 5 + 格式 4）、手机 2/20、性别 1/20、证件类型 1/20、案件类别 2/20、金额 1/76 越界、币种 2/85、日期越界 4 项 —— 均对应夹具注入的脏值，**属预期**。
- **治理告警 2 项**：① 疑似敏感列未声明遮蔽（REQ-D-018，建议补 `policies.json` 的 `property_policies`）；② 金额类属性未声明 unit（`data_elements` 补 元/万元/%）。

## 四、建议处置顺序

1. 定位并修复 demoD 末次构建漏重算 person 的路径 → 重新触发一次含 person 的构建（可用 `--pack`/ANG build_ontology 或直接跑一次 partition 重放），使版本时钟恢复自洽。
2. 补齐 person 是否纳入「银行流水.对方」的口径决策。
3. person clean 链加 `drop_blank`，消除空名实体误链。
4. `data_freshness` 增加未来日期告警。
5. 依 REQ-D-018 决定是否声明敏感列遮蔽与金额单位。

## 五、复验方式

```bash
# 全部在副本上做，不要改原案件
cp cases/demoD/v8.duckdb .workbuddy/probe.duckdb
python - <<'PY'
import duckdb, sys; sys.path.insert(0, r"D:\dev\inves_duckdb")
from core.ontology import build_ontology
con = duckdb.connect(r".workbuddy/probe.duckdb")
print(build_ontology(con, pack="default")["objects"]["person"])   # 期望 27，原库 19
PY
```

体检原始输出在 `.workbuddy/_demoD_report*.txt`，复算脚本在同目录 `_inspect_demoD*.py`。
注：`.workbuddy/` 下留了 5 个临时副本库（约 96MB），本机安全删除机制拦截了自动清理，可手工删除。
