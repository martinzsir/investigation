# REQ-D 附录：B2/B3 能力缺口建设方案

> 来源：`from yuanbao/` 鲁棒性测试（check.md 五批次验收清单）结论中的能力缺口项。
> 状态：**方案定稿，暂缓实施**。实施时按本文档分期执行，验收判据直接对齐 check.md 编号。
> 前置修复（已落地）：红线 R-1 同名实体强证据分区（c53e11c）、脏日期 TRY_CAST 降级（4cda319）、缺列预检 optional_columns（c69aee0），run_tests 73 组全绿。

## 0. 总体原则

1. **声明优先**：新数据源全部走 ontology 案件包 JSON（objects/links/bindings/rules/functions/policies），检测器零改动（三禁令第 1 条）
2. **归一在管道层**：车牌/POI 写法归一前移到 binding `clean`，保证实体代理键（entity 按 name_property 分配）自然去重
3. **判据在规则层**：代持/查封矛盾/同住频次一律 rules.json（`rule_text` + `function/params`），机器只认只读 Function
4. **权限同步**：新对象/链接必须同批写 policies.json（漏了 = fail-closed 运行时被拒），证件号走属性级遮蔽
5. **独立案件包**：新建 `ontology/yuanbao/` 包承载鲁棒性案例的 7 类数据源（`python -m scripts.build_ontology --pack yuanbao`），不污染华创 default 包

---

## 1. B2 地点能力（B2-01/02/03）

**现状**：`core/geo.py` 已有 `normalize_location()`（NFKC/去空白级）与 `_haversine_m()`，`lnk_co_located` 仅字符串精确匹配。缺：POI 主数据、别名字典、路名回退、区划变更。

### 1.1 类型层（objects.json）

```json
{"name": "location", "title": "地点", "pk": "location_id", "kind": "entity",
 "name_property": "std_name",
 "properties": {"std_name": "string", "aliases": "string", "addr": "string",
                "lon": "decimal", "lat": "decimal",
                "region_cur": "string", "region_former": "string", "cat": "string"}}
```

- `aliases` 分号分隔别名表（地点 POI 表是案例 7 源之一，字典即数据）
- 经纬度 decimal → TRY_CAST 兜底脏坐标；可缺列（无坐标 POI）→ `optional_columns`

### 1.2 管道层（bindings.json + links.json）

| 项 | 声明 | 覆盖 |
|---|---|---|
| obj_location binding | source 地点POI表，`optional_columns: ["经度","纬度","曾用区划"]` | B2-03 缺经纬度场景 |
| `lnk_track_at_location`（trackpoint→location） | build_sql：`轨迹地点 = 标准名 OR list_contains(string_split(别名,';'), 轨迹地点)`——别名匹配是**关系定义**，合规放 build_sql | **B2-01 标准化、B2-02 别名归一** |
| `location_match_by_road`（py Function） | 无坐标轨迹点按路名前缀在 obj_location 匹配候选集，输出 `(track_id, matched_location_id, match_type, candidates)`；多候选交规则层裁决 | **B2-03 路名匹配** |
| `location_resolve_region`（py Function） | 按 `region_cur` 或 `region_former`（分号分隔）解析区划下全部地点——**区划变更后旧名仍可查**；py impl 规避 SQL string 参数 enum 白名单限制（区划名是动态集合） | **B2-03 区划变更** |

### 1.3 既有能力升级（唯一 core 代码改动）

`core/geo.py` `locations_colocated()` 解析链升级：有坐标 → haversine 半径（已具备）；无坐标 → 先经 obj_location 别名/路名解析成 location_id 再比对。R4 共现规则与图库自动受益，检测器不改。

### 1.4 验收断言

- **B2-01**：轨迹写"深圳北站(北广场)"与 POI 标准名"深圳北站北广场"→ 命中同一 location_id
- **B2-02**：别名"宝安机场"→ 标准名"深圳宝安国际机场"命中
- **B2-03**：无坐标轨迹"留仙大道XX号"按路名出候选/命中；按"龙华新区"（曾用区划）查到现行区划 POI

---

## 2. B3 车档（B3-01/02/03）

### 2.1 类型层 + 管道层

```json
{"name": "vehicle", "title": "车辆", "pk": "vehicle_id", "kind": "entity",
 "name_property": "plate",
 "properties": {"plate": "string", "brand": "string", "owner_raw": "string",
                "owner_id": "string", "seal_status": "boolean", "seal_date": "date"}}
```

**新清洗规则 `plate_norm`**（`core/ontology.py` CLEAN_RULE_NAMES 注册 + 实现）：去空格/连接符/全角转半角/去"（蓝）"等尾缀——归一在物化前完成，`粤B·12345`/`粤B12345（蓝）`/`粤b12345` 落同一 vehicle_id，**B3-01 由此解决且不需要任何匹配函数**。

### 2.2 规则层（functions.json + rules.json）

| Function | impl | 判据（rules.json `rule_text`） |
|---|---|---|
| `vehicle_user_mismatch` | SQL：同一车牌轨迹主体 ≠ 登记人_raw，按主体聚合次数 ≥ `{{min_tracks}}` | **B3-02 代持**：车辆长期由登记人以外主体使用，疑似代持（标注性线索，不下定性） |
| `post_seal_track_conflict` | SQL：seal_date 非空且查封日期后轨迹数 ≥ `{{min_tracks_after_seal}}` | **B3-03 查封矛盾**：车辆已查封仍出现轨迹记录，列为结构性矛盾信号 |

两条规则 `hit_when: rows_nonempty`，dimension 走"行踪"维、jian_types 复用现有枚举（必要时 enum_space 扩值）。若案例轨迹表不带车牌列，代持降级为 binding `optional_columns` 直通登记人申报列——按 yuanbao 包字段实测取定。

### 2.3 验收断言

- **B3-01**：三种写法 → 1 个 vehicle_id
- **B3-02**：登记人甲/实际主体乙 ≥N 次 → 线索含 rule_text 可审计
- **B3-03**：查封日后轨迹 ≥1 → 矛盾信号线索，溯源行可查

---

## 3. B3 住宿（B3-04/05）

### 3.1 类型层 + 管道层

```json
{"name": "stay", "title": "住宿记录", "pk": "stay_id", "kind": "event",
 "name_property": "guest_raw",
 "properties": {"guest_raw": "string", "guest_id": "string", "hotel": "string",
                "room": "string", "checkin": "date", "checkout": "date",
                "declared_peer": "string"}}
```

- kind=event → 代理键按行分配，同名同证件多行住宿天然不合并
- `declared_peer`（同住人申报列）走 `optional_columns`

**`lnk_co_stay`（stay→stay）**：build_sql 表达关系定义——同酒店同房间 AND 入住/退房区间相交 AND 证件号不同（重叠区间与"不同人"是关系语义，合规）。**端点接 obj_stay 而非 person**：person 级聚合会踩同名代理键碰撞（红线 R-1 阶段二遗留），同人区分靠证件号下沉到 Function 层——`co_stay_pairs`（SQL）：按证件号对聚合同住次数/酒店数 → 规则 **R-同住频次**（≥`{{min_co_stays}}` 出线索）。

### 3.2 B3-05 身份证校验位

py Function `id_card_checksum_valid`（`core/functions.py` `@register_function` 注册）：GB 11643 加权校验，输出 `(证件号, valid)` 行集 → 规则 **R-证件异常**：校验位错误记录落**数据质量线索**（不下定性）；同时为实体对齐提供强证据有效性佐证（衔接红线 R-1 的 id_card 强键）。

### 3.3 权限面（policies.json，与对象同批落地）

```json
{"object": "location", "roles": ["见习","正兵","偏将","主办","human"], "min_clearance": 0},
{"object": "vehicle",  "roles": ["正兵","偏将","主办","human"], "min_clearance": 1},
{"object": "stay",     "roles": ["偏将","主办","human"], "min_clearance": 2}
```

链接 `co_stay`/`track_at_location` 与轨迹同门槛（min_clearance 2）；属性策略 `stay.guest_id` → `mask: partial`（310****1234，主办及以上原文）。

---

## 4. 新增清单汇总

| 层 | 文件 | 增量 |
|---|---|---|
| 类型 | objects.json / links.json（yuanbao 包） | +location/vehicle/stay 三对象；+track_at_location/track_vehicle/co_stay 三链接 |
| 管道 | bindings.json | +3 object bindings（optional_columns 实战化）、+3 build_sql |
| 规则 | rules.json | +R-代持 / R-查封后轨迹 / R-同住频次 / R-证件异常 |
| Function | functions.json + core/functions.py | SQL：vehicle_user_mismatch / post_seal_track_conflict / co_stay_pairs；py：location_match_by_road / location_resolve_region / id_card_checksum_valid |
| 清洗 | core/ontology.py | CLEAN_RULE_NAMES + `plate_norm` |
| geo | core/geo.py | colocated 解析链升级（别名/路名 → obj_location） |
| 权限 | policies.json | 3 对象 + 3 链接 + 1 属性遮蔽 |
| 测试 | tests/test_yuanbao_pack.py（新组 `yuanbaopack`） | 每验收断言一用例（夹具复用 golden load_fixture 模式，案例数据走 build_store） |

## 5. 分期落地（按依赖与回归面排序）

1. **期 1 车档**（最小闭环）：纯声明 + 1 clean rule + 2 SQL Function，无 geo 依赖 → 验 B3-01/02/03
2. **期 2 住宿**：obj_stay + lnk_co_stay + checksum → 验 B3-04/05（含 event 代理键与属性遮蔽）
3. **期 3 地点**（最重）：obj_location + 别名/区划 + geo 解析链升级，回归面最大（golden/R4/图库/MCP）→ 验 B2-01/02/03

每期完成跑 `run_tests.py` 全绿 + 新组；改 MCP 相关加跑 `python -m scripts.mcp_client_test`；期末临时复活五批次探针验证 B2-01/02/03、B3-01..05 转 PASS（取证后删）。

## 6. 遗留与边界说明

- **obj_person 同名代理键碰撞**（红线 R-1 阶段二遗留）：彻底分离需 objects.json 给 person 声明 `key_columns`（类型层变更，涉及代理键策略），单独立项；本方案住宿/同住设计已规避（端点接 stay、同人区分靠证件号）
- **中文日期归一**（B4-07，`2024年3月15日`）：属管道层 `to_date` 清洗规则（CLEAN_RULE_NAMES + 多格式解析），不在本方案范围，建议随期 1 clean rule 一并做
- **B4-06 地址解析降级**（无法解析地址的标注）：依赖本方案 obj_location 落地后才有解析目标，随期 3 补
