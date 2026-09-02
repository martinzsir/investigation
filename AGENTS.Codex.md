# AGENTS.md —— 给 Codex

> 文件名 `AGENTS.Codex.md`。启用时**改名为 `AGENTS.md`** 放到仓库根目录
> （Codex 自动发现 `AGENTS.md`，不认带后缀的名字）。

## 这个项目是什么

孙武侦查官：**确定性侦查推演内核**。跑完出结构化产物，不是聊天助手。

```
data/*.parquet（L3 冷层） → investigation.duckdb（L2 温层） → output/*.json（产物）
data/ladybug/*.lbug（L4 图库，可选）
```

内核 = Python + DuckDB + LadybugDB，纯离线、无大模型依赖。
你（Codex）的角色是**编排与表达**，不碰计算。

## 三条禁令（违反即产物不可信）

1. **不要自己写业务 SQL**
   不要用 `read_parquet` 或直连 DuckDB 拼 SQL 查数据。
   一律走 MCP 工具（`mcp__sunzi__*`）或 `core/` 里的函数。
   理由：LLM 生成的 SQL 无法复现、无法审计，且 2000 亿行进不了上下文。

2. **不要把原始明细搬进上下文**
   只要 `source_rows` 的**数量和溯源 ID**，不要拉全量行。
   `clue_list` 默认已省略 `source_rows`，别主动开 `include_rows=true`。

3. **不要下定性结论、不要置"已立案"**
   内核在工具层强制拦截：`clue_transition` 的 `operator` 拒绝
   `system`/`ai`/`assistant`；置"已立案"必须带 `legal_basis`。
   这不是 prompt 约束，是代码拦截——绕不过去，别尝试。

## 可用 MCP 工具

配好后工具名为 `mcp__sunzi__<name>`（Codex 自动按 server key 命名空间化）。

| 工具 | 用途 | 只读 |
|---|---|---|
| `scan_anomaly` | 扫描候选虚处（只标反常，不给定性） | ✅ |
| `cross_jian` | 五间交叉等级与覆盖度 | ✅ |
| `graph_overpass` | 图库两跳过桥（Cypher+SQL 双轨） | ✅ |
| `clue_list` | 线索摘要（含优先级/状态） | ✅ |
| `clue_transition` | 状态迁移（红线强制） | ❌ |
| `run_pipeline` | 跑全链路（需 `confirm:true`，耗时） | ❌ |

## MCP 配置

`~/.codex/config.toml`：

```toml
[mcp_servers.sunzi]
command = "python"
args = ["-m", "scripts.mcp_server"]
cwd = "/绝对路径/孙武侦查官_DuckDB版"
enabled_tools = ["scan_anomaly", "cross_jian", "graph_overpass", "clue_list", "clue_transition"]
tool_timeout_sec = 120
# run_pipeline 默认不开放：耗时长且改写 output/ 与 DuckDB
```

```bash
codex mcp list          # 确认已注册
codex mcp get sunzi     # 看配置细节
```

## 命令

```bash
python run_tests.py                      # 5 组测试，必须全绿
python run_all.py --no-cli               # 全链路（人工确认）
python run_all.py --auto-review --no-cli # 演示模式（生产禁用）
python -m scripts.mcp_client_test        # MCP 端到端（32 项）
python -m scripts.verify_ladybug         # 图库能力验证
```

## 改完代码怎么验证

**必须**跑 `python run_tests.py`，5 组（graph/org/review/disposal/e2e）全绿才算完成。
改了 MCP 相关还要跑 `python -m scripts.mcp_client_test`。

## 常见坑

- `Store(":memory:")` 是错的——签名是 `(root, db_path)`，内存库要写 `Store(db_path=":memory:")`
- 建图时**先导入全部节点再导入边**，否则 `COPY` 边表报 `Unable to find primary key value X`
- `prioritize_clues()` 返回**新列表**，必须接收返回值，否则顺序不变
- 处置状态改完后要**重新生成** `report`，否则导出的 `by_status` 是旧快照
- 图库 ATTACH DuckDB **不可用**（需联网下载扩展），走 CSV 中转
- 沙箱环境会不定期重置，缺 duckdb 就 `pip install duckdb pandas pyarrow`
