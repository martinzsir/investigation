# AGENTS.md —— 给 DeepSeek Harness (dsh)

> 文件名 `AGENTS.DSH.md`。启用时**改名为 `AGENTS.md`** 放到工作目录。
> dsh 读 `AGENTS.md` 的方式与 Codex 基本一致。

## 这个项目是什么

孙武侦查官：**确定性侦查推演内核**。跑完出结构化产物，不是聊天助手。

```
data/*.parquet（L3 冷层） → investigation.duckdb（L2 温层） → output/*.json（产物）
data/ladybug/*.lbug（L4 图库，可选）
```

内核 = Python + DuckDB + LadybugDB，纯离线、无大模型依赖。
Agent 的角色是**编排与表达**，不碰计算。

## 三条禁令

1. **不要自己写业务 SQL** —— 走工具或 `core/` 函数，理由：不可复现、不可审计
2. **不要把原始明细搬进上下文** —— 只要溯源 ID 和聚合结果
3. **不要下定性结论、不要置"已立案"** —— 工具层强制拦截，绕不过去

## 两种接入方式

### 方式 A：MCP（推荐，与 Codex 共用同一份 server）

dsh 支持 MCP，直接复用 `scripts/mcp_server.py`：

```toml
[mcp_servers.sunzi]
command = "python"
args = ["-m", "scripts.mcp_server"]
cwd = "/绝对路径/孙武侦查官_DuckDB版"
```

一份 server 打通两个 harness，不用写两套。

### 方式 B：dsh 工具插件（TypeScript）

dsh 的工具是 Cordis 插件，三件套 `name` / `inject` / `apply`：

```typescript
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'sunzi-tools'
export const inject = ['tools']

export function apply(ctx: Context) {
  ctx.tools.register(defineTool({
    name: 'scan_anomaly',
    description: '扫描资金/通讯/轨迹异常，返回候选虚处（只标反常，不给定性）',
    parameters: { scope: { type: 'string' } },
    async execute(args) {
      // 调 Python 内核（subprocess 或 HTTP），不要在这里重写计算逻辑
    },
  }))

  ctx.tools.register(defineTool({
    name: 'file_case',
    description: '线索立案（红线：需具名正兵 + 法定依据）',
    parameters: {
      clue_id:     { type: 'string', required: true },
      operator:    { type: 'string', required: true },  // 拒 system/ai
      legal_basis: { type: 'string', required: true },  // 缺则拒
    },
    async execute(args) { /* ... */ },
  }))
}
```

挂载：`dsh web --patch ./sunzi-plugin/cordis.yml`

> **红线写在 `parameters` 的 `required` 里**，让 schema 层先拦一道，
> 再在 `execute` 里二次校验。不要只靠 system prompt 约束模型。

## PTC 模式是这个场景的最佳拍档

dsh 的 **PTC（Programmatic Tool Calling）** 让模型写一段 TypeScript 程序
组合多个工具调用，而不是一轮轮 tool call。

侦查流程天然是多步组合：

```
扫异常 → 交叉升格 → 查图过桥 → 看线索状态 → 生成待办清单
```

一步步 tool call 要 5 轮往返，PTC 一次执行完。**优先用 PTC 编排。**

## 命令

```bash
python run_tests.py                      # 5 组测试，必须全绿
python -m scripts.mcp_client_test        # MCP 端到端（32 项）
python -m scripts.verify_ladybug         # 图库能力验证
dsh --dump-config                        # 查看合并后的插件树
```

## 常见坑

- 沙箱文件系统可能不支持 SQLite journal，SQLite 样本会自动跳过（非代码缺陷）
- 图库 ATTACH DuckDB 不可用，走 CSV 中转；`ladybug` 未装时自动降级 SQL 单轨
- `prioritize_clues()` 返回新列表，必须接收返回值
- 改完处置状态要重新生成 report，否则 `by_status` 是旧快照
- 环境可能缺依赖：`pip install duckdb pandas pyarrow`
