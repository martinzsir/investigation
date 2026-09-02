# AGENTS.md

孙武侦查官：**确定性侦查推演内核**。跑完出结构化产物，不是聊天助手。

## 数据流

```
data/*.parquet（L3 冷层） → investigation.duckdb（L2 温层） → output/*.json（产物）
                                                    └→ obj_*/lnk_*（语义层，声明式编译） → 检测器/图库/MCP
data/ladybug/*.lbug（L4 图库，可选）
```

语义层（Palantir Ontology 裁剪版，schema_version=2 五段）：**声明是数据、实现是代码**——
`ontology/<pack>/*.json` 由 `core/ontology_loader.py` 装载校验
（未知名/版本不符硬失败），`build_ontology()` 编译为 `obj_*` / `lnk_*` 语义表。
**类型层与管道层分离**：
- objects.json / links.json（类型层）：Object/Link 是什么——pk、kind(entity|event)、
  name_property、`properties{属性:值类型}`（string/integer/decimal/date/boolean）；
  值类型经 TYPE_SQL 驱动物化列类型，结构化 source 编译期 CAST，不含任何数据来源信息
- bindings.json（管道层）：object_bindings（source/source_sql/clean/optional）+
  link_bindings（build_sql）；换数据源/新案件改这里，不改检测器代码
- rules.json（规则手册）：自然语言判据 rule_text（分析师写、LLM 经 `rule_list` 读取解释、
  随线索落产物可审计）+ function/params 唯一机器挂钩；`core/rules.py` 确定性执行
  （调只读 Function，不解析自然语言）；LLM 不得自创规则外判据、不写 SQL
- Function（只读）：functions.json 声明 + `core/functions.py` 注册实现；
  SQL 强制 SELECT/WITH 白名单 + `{{param}}` 模板参数（数值类型正则、string 仅 enum 白名单，
  占位符与 parameters 装载期双向核对），检测器只是 Function 的薄编排层
- Action（可写）：actions.json 声明 + `core/action_executor.py` 唯一写路径；
  角色/必填参数/状态机校验，file 副作用创建 obj_decision 决策对象
runtime 对象/链接（obj_decision/lnk_decision_for）在类型层声明属性，
由 `ensure_runtime_tables()` 按声明建表（Action 副作用唯一建表入口；编译器跳过物化、
重建语义层不丢决策）。代理键分两类：entity 按 name_property 值排序分配，
event 事件型按行分配（均幂等）。

内核 = Python + DuckDB + LadybugDB，纯离线、无大模型依赖、无需 API Key。

## 三条禁令

1. **不要自己写业务 SQL** —— 走 MCP 工具或 `core/` 函数（不可复现、不可审计）
2. **不要把原始明细搬进上下文** —— 只要溯源 ID 和聚合结果
3. **不要下定性结论、不要置"已立案"** —— 工具层强制拦截

## 命令

```bash
python run_tests.py                      # 8 组测试，必须全绿
python run_all.py --no-cli               # 全链路（人工确认）
python -m scripts.build_ontology         # 单独构建/重建语义层（obj_*/lnk_*）
python -m scripts.build_ontology --pack <包名>   # 指定 ontology 案件包
python -m scripts.build_ontology --actions      # 查看 Action 注册表
python -m scripts.build_ontology --functions    # 查看 Function 目录（只读计算）
python -m scripts.mcp_client_test        # MCP 端到端（46 项，9 个工具）
python -m scripts.mcp_server             # 启动 MCP server（stdio）
```

WSL2 环境（Ubuntu-24.04，本机已装配，**未明确指定时默认使用此环境**）：

```bash
# venv：/root/.venvs/inves（ladybug/duckdb/pandas/pyarrow/pypinyin/openpyxl，pip 清华镜像见 /etc/pip.conf）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_all.py --auto-review --no-cli"
```

## 验证

改完代码跑 `python run_tests.py`。8 组（mcp / miaosuan / graph / org / review / disposal / ontology / e2e）全绿才算完成。
改了 MCP 相关额外跑 `python -m scripts.mcp_client_test`。

## 已知坑

- `Store(db_path=":memory:")` 才是内存库（签名是 `(root, db_path)`）
- 建图**先节点后边**，否则 `COPY` 边表报 `Unable to find primary key value X`
- 语义层事件型对象（transaction/call/trackpoint，objects.json 里 `kind:"event"`）代理键**按行分配**，实体型（`kind:"entity"`）按 name_property 值分配；改错会让同一主体所有行共享主键
- 图库/SQL 双轨取数走 `_flow_source()`：有 `lnk_transfers` 读语义层，否则回落 `银行流水`——两轨必须同源
- 检测器/图库/MCP **不准直读 Parquet**，一律消费 `obj_*`/`lnk_*`（新数据源加 ontology 案件包 JSON）
- Ontology 声明在 `ontology/<pack>/*.json`（不在 Python 里，schema_version=2）：类型（objects/links）、管道（bindings：source/source_sql/clean/build_sql）、规则（rules：rule_text 自然语言判据 + function/params 挂钩）分文件；值类型/清洗规则名/py function 名/副作用名/binding 与规则交叉引用不存在或不一致时 loader 硬失败；新增 py Function 必须在 `core/functions.FUNCTION_IMPLS` 注册；新数据源加 binding（源列别名必须是已声明属性）；新检测规则加 rules.json（判据写 rule_text、阈值写 params），均不改检测器代码
- SQL Function 的 `{{param}}` 模板参数：integer/decimal/date/boolean 按类型正则渲染，string **仅允许 enum 白名单取值**（自由文本硬失败，防注入）；占位符与 parameters 必须一一对应；链接 build_sql 只表达关系，检测判据一律写 rules/function（lnk_time_window 不含整数/排除公司判据，过滤在 R6 time_window_collision）
- 写操作唯一入口是 `ActionExecutor`（DisposalBoard/MCP 都经它）：新写动作先在 actions.json 声明；Function 只读、永远不准写
- `runtime` 对象/链接（obj_decision/lnk_decision_for）由 Action 副作用创建，编译器跳过；语义层重建不会清掉决策
- `prioritize_clues()` 返回新列表，必须接收返回值
- 处置状态改完要**重新生成** report，否则 `by_status` 是旧快照
- 图库 ATTACH DuckDB：**Windows 原生不可用**（官方 CI 不构建 Windows 版扩展，坏二进制），走 CSV 中转；**WSL/Linux 可用**，手工装配见 INSTALL.md 二.5（`libduckdb.so` 必须放 `~/.lbdb/extension/<版本>/<平台>/common/`，放 /usr/local/lib 无效）
- WSL 环境：pip 必须**用国内镜像**（直连 PyPI 极慢/卡死）；WSL mirrored 断网（DNS 正常、外网 TCP 全断）＝ Windows TUN 的 `0.0.0.0/1`+`128.0.0.0/1` 路由被镜像进来，删除即可（本机 `fix-wsl-routes` 服务已常驻）
- 环境缺依赖时：`pip install duckdb pandas pyarrow`（图库 `pip install ladybug`）

## 更多

- `SKILL.md` —— 技能卡（含 frontmatter，供 agent 发现）
- `INSTALL.md` —— 安装与故障排查
- `AGENTS.Codex.md` / `AGENTS.DSH.md` —— 分 harness 的接入说明
