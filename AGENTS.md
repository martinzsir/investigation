# AGENTS.md

孙武侦查官：**确定性侦查推演内核**。跑完出结构化产物，不是聊天助手。

## 数据流

```
data/*.parquet（L3 冷层） → investigation.duckdb（L2 温层） → output/*.json（产物）
data/ladybug/*.lbug（L4 图库，可选）
```

内核 = Python + DuckDB + LadybugDB，纯离线、无大模型依赖、无需 API Key。

## 三条禁令

1. **不要自己写业务 SQL** —— 走 MCP 工具或 `core/` 函数（不可复现、不可审计）
2. **不要把原始明细搬进上下文** —— 只要溯源 ID 和聚合结果
3. **不要下定性结论、不要置"已立案"** —— 工具层强制拦截

## 命令

```bash
python run_tests.py                      # 7 组测试，必须全绿
python run_all.py --no-cli               # 全链路（人工确认）
python -m scripts.mcp_client_test        # MCP 端到端（32 项）
python -m scripts.mcp_server             # 启动 MCP server（stdio）
```

WSL2 环境（Ubuntu-24.04，本机已装配，**未明确指定时默认使用此环境**）：

```bash
# venv：/root/.venvs/inves（ladybug/duckdb/pandas/pyarrow/pypinyin/openpyxl，pip 清华镜像见 /etc/pip.conf）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_all.py --auto-review --no-cli"
```

## 验证

改完代码跑 `python run_tests.py`。7 组（mcp / miaosuan / graph / org / review / disposal / e2e）全绿才算完成。
改了 MCP 相关额外跑 `python -m scripts.mcp_client_test`。

## 已知坑

- `Store(db_path=":memory:")` 才是内存库（签名是 `(root, db_path)`）
- 建图**先节点后边**，否则 `COPY` 边表报 `Unable to find primary key value X`
- `prioritize_clues()` 返回新列表，必须接收返回值
- 处置状态改完要**重新生成** report，否则 `by_status` 是旧快照
- 图库 ATTACH DuckDB：**Windows 原生不可用**（官方 CI 不构建 Windows 版扩展，坏二进制），走 CSV 中转；**WSL/Linux 可用**，手工装配见 INSTALL.md 二.5（`libduckdb.so` 必须放 `~/.lbdb/extension/<版本>/<平台>/common/`，放 /usr/local/lib 无效）
- WSL 环境：pip 必须**用国内镜像**（直连 PyPI 极慢/卡死）；WSL mirrored 断网（DNS 正常、外网 TCP 全断）＝ Windows TUN 的 `0.0.0.0/1`+`128.0.0.0/1` 路由被镜像进来，删除即可（本机 `fix-wsl-routes` 服务已常驻）
- 环境缺依赖时：`pip install duckdb pandas pyarrow`（图库 `pip install ladybug`）

## 更多

- `SKILL.md` —— 技能卡（含 frontmatter，供 agent 发现）
- `INSTALL.md` —— 安装与故障排查
- `AGENTS.Codex.md` / `AGENTS.DSH.md` —— 分 harness 的接入说明
