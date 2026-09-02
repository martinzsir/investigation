# 孙武侦查官 · 安装指南

> 单机侦查栈安装与验证手册。从零到「全部测试通过」约 10 分钟。
> 技术底座：DuckDB（L2 温层）+ Parquet（L3 冷层）+ LadybugDB（L4 图库，可选）。

---

## 一、环境要求

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Linux / macOS / Windows(WSL2) | 三端实测通过：Ubuntu-24.04（WSL2，Python 3.12.3）与 Windows 原生（Python 3.12） |
| Python | **3.10+**（实测 3.10.12 / 3.12.3） | 3.9 及以下不支持部分类型注解 |
| 磁盘 | ≥ 2 GB 可用 | 含模拟数据与 DuckDB 库文件 |
| 内存 | ≥ 8 GB | 演示数据量仅 29 行，生产按需扩容 |

**硬件选型参考**（真实数据规模）：

| 数据规模 | 内存 | 磁盘 | 形态 |
|---|---|---|---|
| ≤ 30 亿行（验证/中小案） | 64 GB | 2 TB NVMe | 定制笔记本 |
| 50~100 亿行（单案主力） | 128 GB | 4 TB NVMe | 定制工作站本 |
| 200~500 亿行 | 256 GB | 5 TB NVMe | 单路机架 |
| > 1000 亿行 | 需切分布式 | — | 见架构文档 |

---

## 二、安装步骤

### 1. 解压技能包

```bash
unzip 孙武侦查官_完整技能包_v6.zip
cd 孙武侦查官_DuckDB版
```

### 2. 创建虚拟环境（推荐）

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### 3. 安装核心依赖

```bash
pip install duckdb pandas pyarrow
```

> **中国网络环境**：PyPI 直连极慢甚至卡死，建议先配置镜像：
>
> ```bash
> pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
> ```

| 依赖 | 实测版本 | 用途 |
|---|---|---|
| `duckdb` | 1.5.5 | L2 温层：预聚合、物化视图、采样 |
| `pandas` | 2.3.3 | 适配层：多格式读取与标准化 |
| `pyarrow` | 25.0.1 | L3 冷层：Parquet 读写 |

### 4. 安装可选依赖

```bash
# Excel 数据源（.xlsx/.xls）—— 不装则 Excel 文件自动跳过
pip install openpyxl

# 人名拼音相似度 —— 不装则退化为编辑距离（模糊匹配精度略降）
pip install pypinyin

# 图库层（L4）—— Q2 过桥走 Cypher 多跳，不装则自动降级为 SQL 单轨
pip install ladybug

# L1 特征层生产化 —— 不装则自动用 dict 内存模拟
pip install redis
```

> **最小可跑组合**：`duckdb + pandas + pyarrow` 三个即可跑通全部测试组
> （ladybug 未安装时，图库组仍会通过——图库相关用例自动 skip，SQL 轨照常验证）。

### 5. 验证图库能力（可选）

```bash
python -m scripts.verify_ladybug
```

预期输出（**Windows 原生**，4/5）：

```
LadybugDB 版本：0.20.2
  ✅ 建库 + 建节点表 + 建关系表
  ✅ CSV 批量导入 + 基础查询（3 节点）
  ✅ 多跳 MATCH 两跳过桥（1 条路径）
  ✅ 变长跳 [*1..2]（2 个邻居）
  ❌ ATTACH DuckDB 扩展加载
```

预期输出（**Linux / macOS / WSL2**，完成下文手工装配后 5/5）：

```
  ✅ ATTACH DuckDB 扩展加载
  ...
验证结果：5/5 项通过
```

**关于 ATTACH DuckDB**：

- **Windows 原生：不可用**。官方 CI 不构建 Windows 版扩展，发布的是未经测试的坏二进制——
  已实测 libduckdb v1.3.2 / 1.4.5 缺符号、v1.5.2 / 1.5.5 在 LOAD 时崩溃（0xC0000005）。
  属**上游缺陷而非环境限制**，除非自行源码编译，否则直接走 CSV 中转即可，功能无损失。
- **Linux / macOS / WSL2：可用**，但 `INSTALL duckdb` 在受限网络下会卡死，建议手工装配：

  ```bash
  # ① 下载扩展（裸文件直连可达；注意版本号与 ladybug 包大版本对应）
  mkdir -p ~/.lbdb/extension/0.20.0/linux_amd64/duckdb
  curl -L -o ~/.lbdb/extension/0.20.0/linux_amd64/duckdb/libduckdb.lbug_extension \
    https://extension.ladybugdb.com/v0.20.0/linux_amd64/duckdb/libduckdb.lbug_extension

  # ② 下载 libduckdb 原生库（须 v1.5+，与扩展构建版本匹配）
  #    https://github.com/duckdb/duckdb/releases/latest/download/libduckdb-linux-amd64.zip
  #    （WSL 内 curl 下载该资产常失败 exit 56：可在 Windows 侧下载后经 /mnt/c 转入）
  unzip libduckdb-linux-amd64.zip libduckdb.so

  # ③ 关键：加载器只认扩展仓库的 common/ 目录，放 /usr/local/lib 无效
  mkdir -p ~/.lbdb/extension/0.20.0/linux_amd64/common
  cp libduckdb.so ~/.lbdb/extension/0.20.0/linux_amd64/common/
  ```

  验证（对应 `investigation.duckdb` 中的真实表）：

  ```python
  import ladybug as lb

  conn = lb.Connection(lb.Database("/tmp/t.lbug"))
  conn.execute("LOAD duckdb")
  conn.execute("ATTACH 'investigation.duckdb' AS inv (dbtype duckdb)")
  r = conn.execute("LOAD FROM inv.银行流水 RETURN count(*)")
  print(r.get_next())          # → [29]
  ```

不影响使用：即使 ATTACH 不可用，本项目也采用 **CSV 中转**路径，
同样能完成建图与多跳查询：

```
DuckDB 计算 → 导出 CSV → COPY 进 LadybugDB → Cypher 多跳
```

> 注意区分两个"duckdb"：
> - **Python duckdb 包**（`pip install duckdb`）—— 本项目 L2/L3 核心依赖，parquet/json 能力**内置**，无需下载扩展
> - **LadybugDB 的 duckdb 扩展**（`INSTALL duckdb`）—— 让图库能直连 duckdb 文件，需运行时下载

### 6. 验证安装

```bash
python run_tests.py
```

预期输出：

```
>>> [mcp] MCP Server 端到端      ✅ MCP Server 端到端全部通过
>>> [graph] 图库层 Q2 过桥双轨    Ran 12 tests   OK
>>> [miaosuan] 庙算假设引擎      Ran 35 tests   OK
>>> [org] 组织层级对齐            Ran 11 tests   OK
>>> [review] 人工确认工作台       Ran 10 tests   OK
>>> [disposal] 处置状态机+审计链  ✅ 全部测试通过
>>> [ontology] 语义层 Object/Link/Action  Ran 18 tests   OK
>>> [e2e] 端到端集成              Ran 10 tests   OK

✅ 全部通过：mcp, graph, miaosuan, org, review, disposal, ontology, e2e
```

> `mcp` 组会自动重建管线再测试（DuckDB 状态跨会话持久，需重置才能可重复）。

---

## 三、运行管线

### 一键全链路

```bash
python run_all.py --auto-review --no-cli
```

管线十步：

```
0.  数据准备：生成模拟数据 + 初始化 DuckDB
3.  数据接入适配层：多格式样本 → 统一 schema
4.  实体对齐：人名 + 组织层级归并
5.  人工确认工作台：needs_review 候选 → accept / reject / defer
6.  采样预演：1% 采样验证假设方向 → 决定全量
6.5 语义层构建：core/ontology.py 声明 → obj_*/lnk_* 语义表（声明式编译，代理键幂等）
7-8. 侦查主流程：庙算→知己→虚实/奇正/用间 + 血缘去重 + 优先级
8b. 图库两跳过桥（L4）：Cypher 多跳 + SQL 自连接 双轨比对（语义层优先取数）
9.  处置状态：状态机 + 审计链 + 持久化（persist 后重建语义层刷新 obj_clue 快照）
10. 导出操作台数据
```

> `8b` 为可选步骤：`ladybug` 未安装时打印跳过提示，管线继续（降级为 SQL 单轨）。

**参数**：

| 参数 | 说明 |
|---|---|
| `--auto-review` | 演示模式：所有确认候选自动 accept（**生产禁用**） |
| `--no-cli` | 跳过交互式 CLI（CI/测试用） |
| `--operator 姓名` | 确认工作台具名操作者，默认「王检察官」 |

**生产用法**（人工逐条确认）：

```bash
python run_all.py --operator "李检察官"
# 进入交互式工作台：[a]合并 / [r]拒绝 / [d]暂缓 / [q]退出
```

### 其他入口

```bash
python run_demo.py            # 最小演示（六段输出）
python run_with_invoker.py    # 线索流 + 处置 + 采样预演
python run_tests.py --fast    # 跳过端到端，快速回归
python run_tests.py --only org  # 只跑指定组（mcp/miaosuan/graph/org/review/disposal/ontology/e2e）
```

### 语义层与图库边表

```bash
# Ontology 案件包（ontology/<pack>/*.json 声明 Object/Link/Action/Function）
python -m scripts.build_ontology              # 构建/重建语义层 obj_*/lnk_*（幂等）
python -m scripts.build_ontology --pack 包名   # 切换案件包
python -m scripts.build_ontology --actions    # 查看 Action 注册表（角色/参数/副作用）
python -m scripts.build_ontology --functions  # 查看 Function 目录（只读计算）
python -m scripts.export_ladybug    # 从语义层导出 data/ladybug/*.csv（节点+全类边）
python -m scripts.incremental --quarter 2024-Q4   # 增量更新
```

---

## 四、目录结构

```
孙武侦查官_DuckDB版/
├── INSTALL.md / README.md / SKILL.md    # 本指南 / 使用说明 / 技能卡
├── run_all.py            # ★ 单一入口：完整十步管线
├── run_tests.py          # ★ 统一测试入口
├── run_demo.py           # 最小演示
├── run_with_invoker.py   # 线索流 + 处置
├── test_disposal.py      # 处置状态脚本式测试
├── core/                 # 核心引擎
│   ├── store.py          # L1/L2/L3 统一存储接口
│   ├── hypotheses.py     # 庙算：假设生成（数据驱动映射+规则约束+人机协同）+ 知己强制非空
│   ├── registry.py       # skill_invoke 统一调用 + LineageClue 血缘线索
│   ├── lineage.py        # 血缘去重合并 + 优先级排序
│   ├── ontology.py       # 语义层编译器：obj_*/lnk_* 物化（代理键幂等）
│   ├── ontology_loader.py # Ontology 案件包 JSON 装载 + 强校验（声明=数据）
│   ├── functions.py      # Function 层：只读计算执行器（SELECT/WITH 白名单）+ py 实现注册
│   ├── action_executor.py # Action 层：受控写回唯一入口（角色/参数/状态机/决策对象副作用）
│   ├── disposal.py       # 处置看板（状态迁移统一走 ActionExecutor）
│   ├── entity.py         # 组织层级对齐
│   ├── review.py         # 人工确认工作台
│   ├── sampling.py       # 采样预演
│   ├── graph.py          # L4 图库层：LadybugDB 建图 + Cypher 多跳 + 双轨比对
│   └── validate.py       # Schema + 红线校验
├── ontology/             # ★ Ontology 案件包（声明是 JSON 数据，可按包切换）
│   └── default/          # objects.json / links.json / actions.json / functions.json
├── AGENTS.md             # 通用精简版（Codex / dsh 都能读）
├── AGENTS.Codex.md       # Codex 接入说明（用时改名 AGENTS.md）
├── AGENTS.DSH.md         # DeepSeek Harness 接入说明（用时改名 AGENTS.md）
├── skills/               # 五子技能 A-E（庙算/知己/虚实/奇正/用间；检测器=Function 薄编排）
├── scripts/              # init_duckdb / incremental / export_ladybug / build_ontology / dashboard
│   ├── verify_ladybug.py      # 图库能力验证（5 项）
│   ├── q2_overpass_cypher.py  # Q2 过桥 Cypher + SQL 双轨独立演示
│   ├── mcp_server.py          # MCP server（stdio，零第三方依赖，8 工具）
│   └── mcp_client_test.py     # MCP 端到端自测（39 项）
├── data/                 # Parquet 冷层 + 多格式样本 + ladybug 边表
├── tests/                # 单元测试
└── output/               # 运行产物（六段 JSON / 线索 / 处置 / 操作台）
```

---

## 五、接入 Agent（Codex / DeepSeek Harness）

技能包是**确定性内核**，可通过 MCP 暴露给 agent 调用。
内核负责"算"，agent 负责"编排与表达"。

### MCP Server（自研，零第三方依赖）

```bash
python -m scripts.mcp_server          # 启动（stdio，JSON-RPC 2.0）
python -m scripts.mcp_client_test     # 端到端自测（33 项）
```

手写 JSON-RPC 2.0 over stdio，**不依赖 mcp 官方 SDK**，
核心三件套（duckdb/pandas/pyarrow）即可运行；`ladybug` 缺失时自动降级。

暴露 6 个原子工具：

| 工具 | 用途 | 只读 |
|---|---|---|
| `scan_anomaly` | 扫描候选虚处（只标反常） | ✅ |
| `cross_jian` | 五间交叉等级与覆盖度 | ✅ |
| `graph_overpass` | 图库两跳过桥（Cypher+SQL 双轨） | ✅ |
| `clue_list` | 线索摘要（含优先级/状态） | ✅ |
| `clue_transition` | 状态迁移（红线强制） | ❌ |
| `run_pipeline` | 跑全链路（需 `confirm:true`） | ❌ |

### Codex 配置

`~/.codex/config.toml`：

```toml
[mcp_servers.sunzi]
command = "python"
args = ["-m", "scripts.mcp_server"]
cwd = "/绝对路径/孙武侦查官_DuckDB版"
enabled_tools = ["scan_anomaly", "cross_jian", "graph_overpass",
                 "clue_list", "clue_transition"]
tool_timeout_sec = 120
# run_pipeline 默认不开放：耗时长且会改写 output/ 与 DuckDB
```

工具名会被命名空间化为 `mcp__sunzi__<name>`。

### DeepSeek Harness

两种接法，推荐 MCP（与 Codex 共用同一份 server）；也可用 Cordis 工具插件，
其 PTC 模式允许模型写一段 TypeScript 组合多步工具调用，适合本场景的多步侦查流程。

### 红线在工具层强制

不靠 prompt 约束，而是代码拦截：

- `clue_transition` 的 `operator` 拒绝 `system`/`ai`/`assistant` 等占位名
- 置"已立案"必须提供 `legal_basis`，且只能从"已固证"迁移
- 已立案（终态）不可重复迁移
- 所有返回体强制携带 `needs_human_review` 与 `定性_policy`

### 分 harness 说明

- `AGENTS.md` —— 通用精简版
- `AGENTS.Codex.md` —— Codex 接入（MCP 配置 + 常见坑）
- `AGENTS.DSH.md` —— DeepSeek Harness 接入（MCP / Cordis 插件 / PTC）

---

## 六、接入你自己的数据

### 方式一：替换 Parquet（推荐）

按 `data/` 现有列名准备文件，然后重跑初始化：

| 文件 | 必需列 |
|---|---|
| `银行流水.parquet` | 日期、主体、对方、金额 |
| `通话记录.parquet` | 日期、主体、对端、次数 |
| `招投标档案.parquet` | 项目、中标公示日、中标方、分管领导 |
| `工商信息.parquet` | 主体、法人、状态、关联 |
| `轨迹出行.parquet` | 日期、主体、地点 |
| `公开OSINT.parquet` | 主体、公开信息 |
| `举报材料.parquet` | 内容 |

```bash
python -m data.gen_sim       # 或放入你自己的 parquet
python -m scripts.init_duckdb
python run_all.py --auto-review --no-cli
```

> 列名不必完全一致：`build_org_table_from_duckdb` 与 `apply_org_to_duckdb`
> 均内置**列存在性校验**，缺失列自动降级为空证据，不会中断管线。

### 方式二：用适配层导入任意格式

支持 CSV / Excel / JSON / SQLite / Parquet 五类，自动检测分隔符、推断类型、映射中文列名：

```python
from pathlib import Path
from core import Store
from data_ingest import DataIngestManager

store = Store()
ingest = DataIngestManager(store)

# 单文件（带中文列名映射）
ingest.ingest_file(
    "银行流水_2024Q1.xlsx",
    source_type="bank_flow",
    column_map={"交易日期": "日期", "交易金额": "金额",
                "付款方": "主体", "收款方": "对方"},
)

# 批量目录（按文件名自动识别类型）
records = ingest.ingest_directory(Path("raw_data/"))
for r in records:
    print(r["format"], r["source_type"], r["rows"], r["status"])
```

适配层行为：

- **CSV**：自动检测 `,` / `;` / `\t` / `|`；金额自动去除 `,` 与 `¥`；日期自动解析
- **Excel**：多 sheet 自动合并，记录来源 sheet
- **JSON**：递归展开嵌套结构（如 `data.records[*].amount`）
- **SQLite**：整表读取，支持自定义 SQL
- 每份数据保留 `_source_file` 列，全程可溯源

---

## 七、故障排查

### WSL：`pip install` 极慢或卡死

**原因**：PyPI 直连在中国网络环境下极慢。

**处理**：配置镜像（全局一次性）：

```bash
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### WSL：`INSTALL duckdb` 卡死 / GitHub release 下载失败（curl exit 56）

**原因**：扩展服务器与 GitHub 资产重定向目标在部分直连网络下不通（域名可达但下载挂起）。

**处理**：扩展与 libduckdb 均手工装配，见「二、5 关于 ATTACH DuckDB」；
libduckdb 压缩包可在 Windows 侧下载后经 `/mnt/c/...` 转入 WSL。

### WSL mirrored 模式全断网（DNS 正常、LAN 可达、外网 TCP 全断）

**原因**：Windows 侧 VPN/TUN 适配器以 `0.0.0.0/1` + `128.0.0.0/1`（metric 1）劫持全部外网流量，
mirrored 模式会把这两条路由同步进 WSL，导致外网 TCP 死在隧道里（DNS 走 dnsTunneling 不受影响，故症状为"能解析、连不上"）。

**处理**：在 WSL 内删除这两条路由，使默认路由回落真实网关：

```bash
ip route del 0.0.0.0/1
ip route del 128.0.0.0/1
```

路由会在 WSL 重启或 TUN 重连后恢复，可用 systemd oneshot/simple 服务常驻守护（周期清除）。

### `ModuleNotFoundError: No module named 'skills.registry_bootstrap'`

**原因**：工作目录外层存在同名 `skills` 包，遮蔽了本项目包。

**处理**：不要在技能包外层目录放置 `skills/`、`core/` 同名包；本项目已改为按绝对路径加载，若仍出现请检查 `sys.path`。

### `ImportError: Unable to find a usable engine; tried 'pyarrow'`

**原因**：未装 Parquet 引擎。

```bash
pip install pyarrow
```

### `sqlite3.OperationalError: disk I/O error`

**原因**：部分容器文件系统（如 overlay2）不支持 SQLite 的 journal/lock 机制。

**处理**：已内置容错——SQLite 样本自动跳过，其余 4 种格式正常。生产环境请使用 ext4/xfs 本地盘。

### 采样预演显示「命中率 0.00% → 方向否定」

**原因**：小数据集下 `总行数 × 1%` 取整为 0 行，导致采样为空。

**处理**：已修复——`SamplingPreflight` 内置 `min_sample_rows=200` 下限，总量低于该值则自动全量。可调：

```python
SamplingPreflight(store, sample_ratio=0.01, min_sample_rows=200)
```

### `_duckdb.CatalogException: Can only modify view with ALTER VIEW`

**原因**：业务表是 VIEW，无法 `ALTER TABLE` 加 `canonical_org_*` 列。

**处理**：`scripts/init_duckdb.py` 已改为 CTAS 实体表。2000 亿行场景应改回「视图 + 旁路映射表 join」以避免物化开销。

### `Binder Error: Referenced column "统一社会信用代码" not found`

**原因**：不同数据源的工商信息表列名各异。

**处理**：已内置列存在性校验，缺失列自动补空列降级。若需自定义列名：

```python
build_org_table_from_duckdb(
    store, table="工商信息",
    cols={"name": "主体", "legal_rep": "法人", "address": "关联"},
)
```

---

## 八、生产部署检查清单

- [ ] 移除 `--auto-review`，改为人工逐条确认（**AI 严禁自动合并实体**）
- [ ] `--operator` 使用真实具名操作者（禁止 `system` / `AI`）
- [ ] L1 特征层从 dict 换为 Redis：`pip install redis` 并配置 `core/store.py`
- [ ] DuckDB 库文件置于独立 NVMe，与图库 `.lbug` 分盘隔离 IO
- [ ] 开启处置状态持久化（已默认落 DuckDB `clue_disposal_status` 表）
- [ ] 全部线索保持 `needs_human_review=True`，`已立案` 只能经 `已固证` 迁移
- [ ] 定期备份 `investigation.duckdb` 与 `output/` 审计产物

---

## 九、卸载

```bash
rm -rf 孙武侦查官_DuckDB版
deactivate          # 退出虚拟环境
```

---

## 附：一句话验证

```bash
pip install duckdb pandas pyarrow && python run_tests.py
```

输出 `✅ 全部通过：org, review, disposal, e2e` 即安装成功。

---

*本指南对应技能包 v6。技术架构演示用途，不涉及真实办案、不构成侦查指导；*
*AI 不出定性结论，实体合并与立案须由具名正兵依法定程序确认。*
