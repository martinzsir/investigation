# ADR-001：孙武侦查官 SaaS 平台基础架构

**状态**：提议（Proposed）— 待评审
**日期**：2026-09-05
**关联**：REQ-G 治理需求清单（25 项）、ontology_v2 分支

---

## 摘要

为项目开发 Web 平台，使用户通过可视化配置完成数据导入、画像识别、数据接入与业务研判，不直接接触代码。

本 ADR 记录四项架构决策及其技术验证结果。核心结论：

> **采用「案件级版本化文件隔离 + 写队列串行 + 只读并发」的存储架构**，在保留 DuckDB 列存性能与单案件可导出能力的同时，解决多用户并发问题。

---

# 第一部分：四项 P0 决策

## 决策一：部署形态 → 私有化多租户

**结论**：不是公有云 SaaS，而是私有化部署的多租户 Web 平台。

**理由**：系统处理公安涉案数据——人员身份证、轨迹、住宿、通话、银行流水。这类数据在绝大多数司法辖区不允许出公安网。这不是技术判断，是数据性质决定的。

**连锁影响**：
- 不能使用云厂商托管数据库（或仅能用私有化版本）
- 多租户隔离需在应用层自行实现，不依赖云平台 IAM
- 升级维护依赖镜像/包分发，非滚动发布
- 需考虑内网/离线环境的依赖打包（DuckDB 嵌入式在此占优）

**意外收益**：私有化 + DuckDB 嵌入式意味着**单案件可整体导出为一个文件包**（DuckDB 文件 + Parquet + 声明文件）。案件移交、上级调阅、离线研判均可直接使用。这是公有云方案无法提供的能力。

---

## 决策二：存储与并发 → 案件级文件隔离 + 写队列串行 + 只读并发

### 问题本质

DuckDB 并发模型：单进程多连接可并发读，**写独占**；多进程写同一文件会冲突。

项目访问模式呈现极端读写分化：

| 类型 | 操作 | 频率 | 耗时 |
|---|---|---|---|
| 写 | 数据导入、语义层重建、全量扫描 | 低 | 长（秒~分钟） |
| 读 | 线索列表、健康度、审计链、仪表盘 | 高 | 短（毫秒~百毫秒） |

### 方案对比

| 方案 | 代价 |
|---|---|
| **A. 案件级文件隔离 + 写队列** | 跨案件查询需 ATTACH（已验证可行） |
| B. 换 Postgres | 失去列存分析性能；需重写大量 SQL；丢失 Parquet 直读 |
| C. 单文件 + 全局写锁 | 一个用户导入数据，全平台阻塞 |

### 选择 A 的四条理由

**1. 顺着项目设计取向，而非逆着。** 项目 README 明确记录「用 DuckDB 单文件替代 StarRocks 承担 L2 温层，零运维、与主进程同进程」。这是一次从分布式回退到嵌入式的主动迁移。换成 Postgres 等于把该迁移倒回去，且是为了解决一个可通过文件隔离绕开的问题。

**2. 案件级隔离匹配业务边界。** 侦查业务中"跨案件并发写同一库"几乎不存在——不同案件由不同侦查员办理。真正会冲突的是"同案件内多人同时操作"，恰由任务队列解决。且审计链本身是 per-case 的（`audit_chain.case_id` 必填），数据结构上已对齐。

**3. 写任务本就该异步。** 语义层重建与全量扫描可能耗时数十秒至数分钟。CLI 下用户可等待，Web 下必须异步。既然要做任务队列，串行化几乎零额外成本。

**4. 保住单案件导出能力。** 一个案件 = 一个 DuckDB 文件 + 一批 Parquet + 一套声明文件，可直接拷贝、归档、移交。

### 唯一实质代价：跨案件查询

案件级隔离后，跨案件串并分析变难。**缓解手段**：DuckDB 支持 `ATTACH` 多库做跨库联合查询（已验证，见第四部分）。

建议：预留跨案件通道，但**不作为主路径**——主路径仍是单案件分析。

---

## 决策三：租户与本体边界 → 平台共享 pack + 案件隔离数据

**结论**：平台级共享本体（pack）+ 租户/案件级隔离数据（case）。

**概念对齐**（项目已有）：
- `pack`（`load_pack("default")`）= 模型定义，含 13 个声明文件
- `case_id`（审计链字段）= 案件实例

**理由**：本体可复用才是平台核心价值。一个"电信诈骗研判模型"配好后应能被多个案件套用。若每个案件从头配置，平台仅是"能跑的 JSON 编辑器"，失去积累效应。

**层次设计**：

```
平台层   共享本体库（pack）：电信诈骗模型 / 贪污贿赂模型 / ...
   ↓ 派生
租户层   单位级 pack 副本（可定制，不改平台原始）
   ↓ 实例化
案件层   案件数据 + 案件级 pack 快照（保证可复现）
```

**关键约束：案件必须锁定 pack 快照版本号。**

审计链 `ontology_version` 已在记录（实测 `ver=2.11.10`）。若共享本体在案件分析后被修改，历史结论无法回答"依据哪一版规则"。该版本号必须指向**不可变快照**。

**反向约束**：若有"不同单位本体必须完全不可见"的强需求，则需改为租户级 pack，移除平台共享层。

**折中建议**：平台层放脱敏通用模型骨架（不含真实人名、地名、案件细节），租户层定制。**知识包（`case_knowledge.json`）永远只放案件层**——它天生携带真实实体。

---

## 决策四：代码逃生舱 → 平台生成代码桩 + 人工合入

**结论**：不做租户沙箱在线执行自定义代码。

**必须写代码的扩展点**：

| 扩展点 | 卡在何处 |
|---|---|
| 新 py Function | 需在 `FUNCTION_IMPLS` 注册 |
| 新值类型 | `TYPE_SQL` 仅 5 种 |
| 新清洗规则 | `CLEAN_RULE_NAMES` 白名单 |
| 新 Action 副作用 | `side_effects` 目前两种 |

**不做沙箱的三条理由**：
1. 安全边界极难做实（沙箱逃逸、资源耗尽）
2. 侦查系统放行任意代码执行，审计上不可接受
3. 代码质量无法保证，慢查询可拖垮平台

**正确做法——做成增长飞轮**：

```
用户提需求 → 平台生成代码桩（签名+契约+测试骨架）
    → 开发者审核、实现、补测试
    → 合入平台函数库，成为可复用资产
    → 后续同类需求 → 纯配置，不再需要代码
```

飞轮转速决定平台成熟度。**函数库是唯一真正的护城河**——竞品可复制界面，无法复制领域函数积累。

**配套设计**：统计"哪些需求反复触发逃生舱"。若某类需求高频出现（如各种格式的地点解析），说明应抽象为配置化能力或通用 Function。该统计是产品路线图的输入。

---

# 第二部分：任务队列设计

## 2.1 幂等性基线

**硬原则：所有写任务必须可安全重试。**

现有代码中两个最重的写操作已达标：
- `build_ontology` 注释明确「幂等，可重跑」
- `run_incremental` 按分区幂等跳过（AC4：同分区同内容已应用过则跳过）

| 任务类型 | 操作 | 幂等性 |
|---|---|---|
| `IMPORT` | 原始→Parquet 冷层+编目 | 需改造（按 source 指纹） |
| `BUILD` | `build_ontology(conn, pack)` | ✅ 已幂等 |
| `SCAN` | 规则扫描/全管线 | 需改造（按 run_id 分区） |
| `EXPORT` | 导出案件包 | 天然幂等 |
| `ARCHIVE` | 冻结归档 | 天然幂等 |

幂等键统一为 `(case_id, task_type, input_fingerprint)`。

## 2.2 任务表结构

```sql
CREATE TABLE task (
    task_id         BIGSERIAL PRIMARY KEY,
    case_id         TEXT        NOT NULL,
    task_type       TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    idempotency_key TEXT        NOT NULL,

    operator        TEXT        NOT NULL,   -- 真实登录用户，非占位符
    params          JSONB       NOT NULL DEFAULT '{}',
    reason          TEXT,                   -- 高危任务必填，进审计

    target_version  INTEGER,
    base_version    INTEGER,

    progress_pct    SMALLINT    NOT NULL DEFAULT 0,
    progress_stage  TEXT,
    progress_detail JSONB,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,

    error_code      TEXT,
    error_message   TEXT,
    result          JSONB,

    retry_count     SMALLINT    NOT NULL DEFAULT 0,
    max_retries     SMALLINT    NOT NULL DEFAULT 2,

    CONSTRAINT uq_task_idem UNIQUE (case_id, task_type, idempotency_key)
);
CREATE INDEX idx_task_queue ON task (case_id, status, created_at);
```

**`uq_task_idem` 唯一索引让幂等由数据库保证**，而非应用层判断。

## 2.3 两级队列

```
┌──────────── 全局 Worker 池（并发上限 N=4）────────────┐
│  case_A: [BUILD(v6)] → [SCAN] → [EXPORT]   案件级 FIFO │
│  case_B: [IMPORT] → [BUILD(v3)]            案件级 FIFO │
│  case_C: [SCAN]                            案件级 FIFO │
└────────────────────────────────────────────────────────┘
```

- **第一级 案件级 FIFO**：同案件写任务严格串行，保证文件写独占语义
- **第二级 全局 Worker 池**：限制并发写任务总数（文件隔离了，CPU 没有）

调度逻辑：

```
每轮：
  1. 若运行中任务数 >= N，等待
  2. 找出所有"有 PENDING 且无 RUNNING"的案件
  3. 按 created_at 取最老的 PENDING 任务派发
```

保证同案件串行 + 跨案件轮转公平（老任务优先，避免饿死）。

## 2.4 状态机

```
PENDING → RUNNING → SUCCEEDED
             │    ↘ FAILED（retry_count ＜ max_retries → 回 PENDING）
             └─→ CANCELLED
```

**失败重试三要点**：
1. **版本文件先丢弃**：BUILD 失败时删除半成品 `case_A_v6.duckdb`，从 v5 重建
2. **指数退避**：`delay = base * 2^retry_count`
3. **不重试语义性错误**：缺列、权限拒绝、声明非法重试无意义，直接 FAILED 并推原因给用户

## 2.5 进度事件

```json
{
  "task_id": 12345,
  "pct": 45,
  "stage": "xu_shi",
  "stage_label": "虚实",
  "detail": {
    "rules_total": 7,
    "rules_done": 3,
    "current_rule": "R4 轨迹同框",
    "findings_so_far": 12
  },
  "ts": "2026-09-05T14:30:22Z"
}
```

- **回报方式**：Worker 写 `task.progress_*` + PG NOTIFY；前端 SSE 订阅（进度是单向流，SSE 比 WebSocket 简单且支持断线重连）
- **节流**：阶段边界回报 + 最小间隔 500ms

## 2.6 失败语义

```
任务「重建语义层」失败
当前数据仍是 v5（2026-09-05 14:30 构建，可正常查询）
失败原因：bindings.json 引用了不存在的列「项目地点」
```

用户看到的是"分析功能未受影响，只是本次重建未生效"——**版本化最直观的收益**。

## 2.7 与审计链的关系

| | 任务表 | 审计链 |
|---|---|---|
| 载体 | Postgres | DuckDB `audit_chain` |
| 记录 | 导入/构建/扫描等系统操作 | 线索状态迁移等业务动作 |
| 保留 | 可定期清理 | **永久，不可删** |

任务完成时若产生业务状态变更，由 `ActionExecutor` 写入审计链（现有 `_chain()` 已实现）。

---

# 第三部分：Store 抽象接口

## 3.1 迁移压力（实测代码核查）

| 核查项 | 数量 | 处理 |
|---|---|---|
| `Store(...)` 总实例化 | 101 处 | 逐个核对是否显式传参 |
| `Store()` **无参** | **19 处** | **必须改**，默认路径假设深嵌业务 |
| `duckdb.connect` 直连 | 8 处 | **必须收口**，否则租户隔离漏 |
| Redis | 无硬依赖（`l1` 是 dict） | 可直接替换 |
| LadybugDB | 仅 `core/graph.py` | 可独立旁路 |

## 3.2 接口定义

```python
class StoreBackend(ABC):
    @abstractmethod
    def read_conn(self) -> Any:
        """只读连接（可并发）。跨案件实现返回 ATTACH 后连接。"""

    @abstractmethod
    def write_conn(self) -> Any:
        """写连接（独占）。跨案件实现抛 UnsupportedOperation。"""

    @abstractmethod
    def query(self, sql, params=(), *, unsafe=False, reason=None,
              operator=None, max_rows=1000) -> list[dict]:
        """保留现有签名语义（REQ-003 unsafe 通道不变）。"""

    @property
    @abstractmethod
    def version(self) -> int: ...

    @property
    @abstractmethod
    def case_id(self) -> str: ...


class CaseStore(StoreBackend):
    def __init__(self, case_id: str, version: int, *, mode: str = "read"):
        ...
    # mode="read"  → duckdb.connect(path, read_only=True)
    # mode="write" → duckdb.connect(path)  独占，由任务队列保证串行


class CrossCaseStore(StoreBackend):
    def __init__(self, cases: dict[str, int], *, access, reason: str):
        ...
    # ATTACH 多版本文件，全部 READ_ONLY
    # write_conn() → raise UnsupportedOperation
```

## 3.3 关键设计决策

**决策一：`Store` 保留为门面，内部委托 backend**

现有 101 处调用持有 `Store` 实例，强制换类型改动面过大。

```python
class Store:
    """兼容门面：保留现有 API，内部委托 backend。"""
    def __init__(self, backend: StoreBackend):
        self._backend = backend

    @property
    def conn(self):
        return self._backend.read_conn()   # 默认只读，避免误写
```

**默认给只读连接是安全设计**——多数 `store.conn` 用途是查询，需写的路径显式取 `write_conn()`。未改造的老调用点也不会意外获得写权限。

**决策二：工厂方法替代无参构造**

19 处 `Store()` 改为：

```python
store = store_factory.for_case(case_id, mode="read")
store = store_factory.for_cross_case([...], access=ctx, reason="...")
```

工厂负责：查版本号、拼路径、开连接、注册上下文以便回收。

**决策三：8 处直连按性质分别处理**

| 位置 | 处理 |
|---|---|
| `scripts/init_duckdb.py` | 改走 `CaseStore(mode="write")` |
| `scripts/incremental.py` | 同上 |
| `scripts/export_dashboard.py` | 改走 `CaseStore(mode="read")` |
| `benchmarks/benchmark.py` | 保留直连（不进生产路径） |
| `tests/test_*.py` | 保留直连（用 `:memory:`，不需文件隔离） |

## 3.4 连接生命周期

```
请求进入 → 工厂开 read_conn（read_only）
        → 业务逻辑 → 请求结束 → close()

任务开始 → 工厂开 write_conn（独占，指向新版本文件）
        → 构建/导入
        → 成功：切指针 → close()
        → 失败：删半成品 → close()
```

**只读连接必须及时关闭**——只要有一个读者持有句柄，写者就抢不到锁（已由 H1/H7 验证）。用请求作用域管理 + 连接池复用，请求结束必须归还。

## 3.5 L1 热层

现有 `self.l1: dict` 带注释「生产换 Redis」，无硬依赖 → 直接替换：

```python
@property
def l1(self) -> MutableMapping[str, Any]:
    """热层。CaseStore 返回 Redis 包装；CrossCaseStore 返回空实现。"""
```

---

# 第四部分：S5 与 S7 技术验证

> **重要说明**：本节数据来自 2026-09-05 在沙盒环境的实际执行，脚本为 `spike_s5.py` / `spike_s7.py`，可复跑。
> 环境：DuckDB 1.5.5 / Python 3.10.12。

## 4.1 S5：版本化 + 原子切换

### 假设与实测

| 编号 | 假设 | 结果 |
|---|---|---|
| H1 | 读者持 vN 句柄时，写者**不能**就地更新 vN | ✅ 成立 |
| H2 | 读者持 vN 时，写者**能**构建 v(N+1) | ✅ 成立 |
| H3 | 切换指针后，旧读者 vN 句柄**仍可读** | ✅ 成立 |
| H4 | 写 v(N+1) 失败时，vN **完好无损** | ✅ 成立 |

**实测输出**：

```
[准备] 构建 v5: rc=0 BUILT caseA_v5.duckdb（1000 行）

【H1】读者持有 v5 只读句柄时，写者就地更新 v5
  实际: rc=1（写入失败，锁冲突）
  → H1 成立：就地更新会受读者影响

【H2】读者持有 v5 句柄时，写者构建 v6（新文件）
  实际: rc=0 BUILT caseA_v6.duckdb
  → H2 成立：版本化解除读写互斥

【H3】指针切换到 v6 后，旧读者的 v5 句柄仍可读
  实际: rc=0 READ_OK 1000
  → H3 成立

【H4】构建新版本失败后，当前版本 v5 完好
  v5 存在=True，半成品已清理=True，行数 1000→1000
  → H4 成立：零成本原子回滚
```

**四个假设全部成立，架构地基验证通过。**

### 一个必须注意的细节（H3b 补充验证）

额外测试了"切换后立即删除旧版本文件"的行为：

```
【H3b】删除被持有句柄的文件后读取
  删除前读到 100 行，删除后仍读到 100 行
  → POSIX 语义下句柄仍有效
```

**这说明我原先的判断过于绝对。** 在 Linux/POSIX 下，`unlink` 一个已打开的文件，持有句柄的进程仍可读取（inode 在引用计数归零前不释放）。

但这**不能**作为"可以立即删除"的依据，理由有三：

1. **平台依赖**：Windows 上删除被打开的文件会失败，行为与 POSIX 不同
2. **空间不释放**：POSIX 下文件虽"删除"，磁盘空间在句柄关闭前不会回收。立即删除并**不能**达成节省存储的目的
3. **重启后残留风险**：进程崩溃时无法追踪哪些文件被 unlink 但未释放

**修正后的结论**：版本回收仍应延迟到读者排空，但理由从"否则存量读者会崩溃"修正为"跨平台一致性 + 空间实际回收 + 崩溃可恢复性"。

### 验收标准（给开发）

```python
def test_version_switch():
    # GIVEN 案件处于 v5，有活跃读者
    reader = open_read_only(v5)
    # WHEN 写者构建 v6
    result = build_task(case, target_version=6)
    # THEN
    assert result.success
    assert reader.query("SELECT COUNT(*) FROM obj_person")  # 旧句柄仍可读
    assert get_current_version(case) == 6

def test_write_failure_rollback():
    # GIVEN v5 正常，读者活跃
    # WHEN 构建 v6 时注入失败
    # THEN
    assert get_current_version(case) == 5   # 指针未动
    assert not exists(v6_path)              # 半成品已清理
    assert reader.query(...)                # 读者不受影响
```

---

## 4.2 S7：跨案件 ATTACH 通道

### 假设与实测（50 库 × 1000 行）

| 编号 | 假设 | 结果 |
|---|---|---|
| H5 | ATTACH 期间，写者能构建被 ATTACH 库的**新版本** | ✅ 成立 |
| H6 | ATTACH 20~50 库后跨库查询性能可接受 | ✅ 成立 |
| H7 | ATTACH 期间，被 ATTACH 的库**不能**被就地写 | ✅ 成立 |

**实测输出**：

```
【H7】ATTACH 只读期间，就地写被 ATTACH 的库
  实际: IOException: Could not set lock on file
        "case_000_v1.duckdb": Conflicting lock is held
  → H7 成立

【H5】ATTACH 只读期间，写者构建新版本文件
  实际: 新版本构建成功 -> case_000_v2.duckdb
  → H5 成立

【H6】ATTACH 50 个库的性能
  ATTACH 50 个库:        0.02s
  跨库 UNION ALL 查询:   0.010s（总行数 50000）
  跨库聚合:              0.010s
  进程峰值 RSS:          132 MB
  → H6 成立
```

### 最重要的发现：版本化是 S7 的前提，不是优化

H7 证明：跨案件查询期间，被 ATTACH 的库**无法就地写入**。

若无版本化，一个跑几十秒的全省串并分析会**同时锁死 50 个案件的所有写操作**。

版本化让写者构建新版本文件（不同 inode），与 ATTACH 的只读句柄互不干扰（H5）。

> **实施顺序硬约束：S5 必须先于 S7 完成。否则 S7 做了也不可用。**

### 性能解读

ATTACH 50 库耗时 0.02s，查询 0.010s，RSS 132 MB。性能不是瓶颈。

但仍建议对连接做缓存——真实场景下库文件更大，ATTACH 开销会上升：
- 按 `case` 组合缓存 ATTACH 连接，相同组合复用
- 高频组合（如"某单位全部在办案件"）维护长连接
- 超过 50 库的查询显式警告用户或改走异步任务

### 未验证的边界

本次测试规模为 50 库 × 1000 行的小规模。真实场景可能有单库数百万行、数 GB 文件。

**建议追加一轮接近真实规模的验证**，特别是 ATTACH 大文件时的内存占用与地址空间压力（DuckDB 为内存映射式）。

---

## 4.3 跨案件通道的四条硬约束

即使技术上可行，跨案件查询是高风险操作，必须设限：

**约束一：权限全有或全无**

用户必须对**所有**请求案件都有访问权限，否则整个查询拒绝。SQL 的 JOIN 与聚合会让数据从无权案件"泄漏"到有权案件结果中——部分放行等于全部放行。

**约束二：绝不绕过策略引擎**

`OntologyReadGateway` 注释明确：「读视图唯一入口是 `OntologyReadGateway.view(name)`（AC4：不绕过 REQ-002 网关）」。`views.json` 亦声明「视图不复制权限事实，策略仍由 PolicyEngine 在读时执行」。

跨案件通道必须走同一套 `PolicyEngine`。ATTACH 只让数据可见，权限判定仍在应用层。

**约束三：只读 + 限额**
- ATTACH 一律 `READ_ONLY`
- 强制 `max_rows` 上限（现有 `Store.query` 已有 `max_rows=1000` 硬限制）
- 强制超时
- 禁止 DDL/DML

**约束四：全程审计**

跨案件查询本身进审计链，记录：操作人、涉及案件列表、SQL 原文、reason。事后必须可追问"谁在何时把哪几个案子并在一起查了"。

---

# 第五部分：案件包导出格式

## 5.1 四个设计目标（按重要性）

1. **可复现** — 任何人拿包在任何机器上跑，得到同样结果
2. **自包含** — 不依赖原平台任何外部服务
3. **可验证** — 能证明内容未被篡改
4. **可读** — 人能直接看懂结构，不靠工具

## 5.2 目录结构

```
case_2024_001_v07_20260905/
├── manifest.json              # 包清单：版本、校验和、元信息
├── README.md                  # 人读说明：怎么打开、怎么复现
│
├── ontology/                  # 声明快照（13 个文件，不可变）
│   ├── objects.json  bindings.json  links.json
│   ├── rules.json    functions.json thresholds.json
│   ├── policies.json views.json     dimensions.json
│   ├── enum_space.json  actions.json  llm_policy.json
│   └── case_knowledge.json    # 含真实实体，标 sensitive
│
├── data/
│   ├── cold/                  # L3 冷层：原始 Parquet（保留 _source_file）
│   │   └── 银行流水.parquet ...
│   └── case.duckdb            # L2 温层：语义表（版本压实后单文件）
│
├── graph/
│   └── investigation.lbug     # 图库（若存在）
│
├── audit/
│   ├── chain.csv              # 审计链导出（人可读）
│   └── chain_verify.json      # 完整性校验结果
│
└── output/
    ├── lineage_clues.json     # 线索产物
    ├── health.json            # 健康度
    └── dashboard/             # 可视化快照
```

## 5.3 manifest.json

```json
{
  "format": "sunwu-case-package",
  "format_version": "1.0",

  "case_id": "case_2024_001",
  "case_name": "王秀英被诈骗案",
  "exported_at": "2026-09-05T14:30:00Z",
  "exported_by": "王检察官",
  "export_reason": "案件移交",

  "ontology": {
    "pack": "case_2024_001",
    "version": "2.11.10",
    "file_count": 13,
    "files": {
      "objects.json":  {"sha256": "a1b2c3...", "bytes": 8192},
      "case_knowledge.json": {"sha256": "...", "bytes": 2048, "sensitive": true}
    }
  },

  "data": {
    "duckdb_file": "data/case.duckdb",
    "duckdb_sha256": "...",
    "duckdb_bytes": 10485760,
    "cold_files": [
      {"path": "data/cold/银行流水.parquet", "sha256": "...", "rows": 1200}
    ],
    "graph": {"present": true, "path": "graph/investigation.lbug", "sha256": "..."}
  },

  "audit": {
    "chain_records": 11,
    "chain_root_hash": "9f8e7d...",
    "chain_verified": true,
    "verified_at": "2026-09-05T14:29:58Z"
  },

  "reproduce": {
    "engine_version": "2.11.10",
    "commands": [
      "python -m scripts.verify_package case_2024_001_v07_20260905/",
      "python -m scripts.build_ontology --pack case_2024_001",
      "python run_all.py --auto-review --no-cli"
    ]
  }
}
```

## 5.4 三个关键设计点

**设计点一：导出前必须做版本压实**

案件历史可能积累 v1..vN 多个版本文件。归档时只保留最后一个。

正确顺序：
1. 等待所有读任务排空
2. 复制当前版本为新基线文件
3. 校验新文件完整性
4. 切换指针
5. 删除历史版本

**设计点二：审计链必须导出前校验并冻结**

`manifest.audit.chain_verified` 必须是**导出那一刻的真实校验结果**，不能事后填。

现有 `chain_integrity()` 已返回 `{chain_ok, expected_count, actual_count, broken_links, missing_fields, disposal_events, disposal_activity}`——原样写入 `audit/chain_verify.json`。

**若 `chain_ok=false`，导出应告警但允许继续**（强行阻断会导致无法导出），但必须醒目标记。

**设计点三：`case_knowledge.json` 标敏感**

实测 `policies.json` 中交通违章对象标注「含行踪与车辆信息」。知识包装的是真实人名、地名、关系断言——包内敏感度最高。

manifest 标 `"sensitive": true`，提示接收方按密级管理。将来实现脱敏导出时，此文件是首要处理对象。

## 5.5 导入与验证

```bash
python -m scripts.verify_package ＜包路径＞
```

流程：
1. 校验 `format` 与 `format_version` 是否支持
2. 逐文件算 SHA-256 与 manifest 比对（**任一不符即失败**）
3. 检查 13 个声明文件齐全且 `schema_version` 一致（REQ-G-016 已保证版本收口）
4. 校验审计链 root_hash
5. 尝试以 `read_only` 打开 DuckDB，确认文件未损坏

通过后用现有 `PackManager.init_pack(from_pack=)` 导入。

---

# 第六部分：落地顺序

| 阶段 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| **S1** | 消灭 19 处无参 `Store()` + 收口 8 处直连 | 无 | 低，纯机械 |
| **S2** | 定义 `StoreBackend` 接口，`Store` 改门面 | S1 | 中，101 处调用需回归 |
| **S3** | 元数据层（Postgres）+ 案件生命周期 | S2 | 中 |
| **S4** | 任务队列 + Worker 池 | S3 | 中 |
| **S5** | **版本化文件 + 原子切换** | S2+S4 | **高，核心机制（已验证）** |
| **S6** | 案件包导出/导入 | S5 | 低 |
| **S7** | 跨案件 ATTACH 通道 | **S5（硬依赖）** | 高（已验证小规模，待大规模验证） |

**S5 是分水岭**——前面是铺垫，后面都依赖它。

**S7 必须在 S5 之后**（H7 证明无版本化时 ATTACH 会锁死写）。

---

# 第七部分：风险登记

| 风险 | 影响 | 处置 |
|---|---|---|
| ATTACH 大规模性能 | 跨案件查询慢或内存压力 | 小规模已验证（50 库/132MB）；**待 GB 级验证** |
| 单案件文件过大 | 导入/构建慢，迁移困难 | 冷层 Parquet 已分流；监控 + 归档策略 |
| 队列积压 | 多用户同时扫描 | 案件级 FIFO + 全局并发上限 + 优先级 |
| 版本文件堆积 | 存储膨胀 | 延迟回收（读者排空后）；归档时压实 |
| **孤儿版本文件** | Worker 被 kill -9 时半成品残留 | **需启动时扫描清理机制（未验证，建议专项测试）** |
| 元数据层单点 | 平台级不可用 | Postgres 主从；元数据量小，恢复快 |
| 跨 Windows 部署 | 版本回收语义差异 | 不依赖"立即删除"，一律走延迟回收 |

---

# 附录 A：验证脚本

| 脚本 | 用途 | 可复跑 |
|---|---|---|
| `/data/workspace/spike_s5.py` | 版本化四假设（H1-H4 + H3b） | ✅ |
| `/data/workspace/spike_s7.py` | ATTACH 三假设（H5-H7） | ✅ |

运行：

```bash
cd /data/workspace
PYTHONPATH=/data/workspace/pylibs python3 spike_s5.py
PYTHONPATH=/data/workspace/pylibs python3 spike_s7.py
```

环境：DuckDB 1.5.5 / Python 3.10.12。

---

# 附录 B：本 ADR 的数据来源与可信度声明

| 内容 | 来源 | 可信度 |
|---|---|---|
| 四项 P0 决策 | 基于项目代码与设计文档推理 | 设计判断，需评审确认 |
| 任务队列/Store/导出格式设计 | 基于现有代码结构推导 | 设计，未实现 |
| 代码耦合数据（19/101/8 处） | 静态 grep 核查 | **实测，可复现** |
| S5 四项假设 | spike 实跑 | **实测，可复跑** |
| S7 三项假设 | spike 实跑（50 库小规模） | **实测小规模；大规模待验证** |
| ATTACH 性能数值 | spike 实跑 | **实测（50×1000 行）** |

**需明确说明的局限**：

1. S7 性能数据来自 50 库 × 1000 行的小规模测试。真实场景下库可能达数 GB，ATTACH 的内存与地址空间压力**尚未验证**。
2. 孤儿版本文件清理、Worker 被强杀后的恢复，**未验证**，建议专项测试。
3. 本 ADR 未覆盖前端架构、API 设计、认证授权体系、多租户计费与配额等，这些是 P1 之后的内容。

---

*本 ADR 为技术架构提案，不涉及真实办案数据。*

