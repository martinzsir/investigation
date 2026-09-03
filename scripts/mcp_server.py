"""
scripts/mcp_server.py
孙武侦查官 · MCP Server（stdio 传输）

手写 JSON-RPC 2.0 over stdio，**不引入任何第三方依赖**——
只有核心三件套（duckdb / pandas / pyarrow）就能跑，图库不可用时自动降级。

协议依据（已核对 MCP specification 2025-06-18）：
  · 传输：newline-delimited JSON-RPC over stdin/stdout
           stdout 只能输出 MCP 消息，日志一律走 stderr
  · 生命周期：initialize → notifications/initialized → tools/list → tools/call
  · 通知（无 id 字段）不回响应
  · 错误码：-32700 解析 / -32600 无效请求 / -32601 方法不存在
            -32602 参数无效 / -32603 内部错误

暴露的工具（6 个原子能力）：
  1. scan_anomaly      虚实扫描：标出候选虚处（只读，不给定性）
  2. cross_jian        五间交叉：给出交叉等级与覆盖度
  3. graph_overpass    图库两跳过桥（Cypher + SQL 双轨，无 ladybug 时降级）
  4. clue_list         线索列表（含优先级/状态，只返回摘要不返回明细）
  5. clue_transition   线索状态迁移（红线：禁止直接置"已立案"）
  6. run_pipeline      跑全链路（受控，默认禁用）

红线在工具层强制，不靠 prompt 自觉：
  · clue_transition 的 operator 拒绝 "system"/"ai"/"assistant"
  · 置"已立案"必须带 legal_basis 且只能从"已固证"迁移
  · 所有工具返回结构强制带 needs_human_review 与 定性_policy 字段

用法：
    python -m scripts.mcp_server
    # 或配进 ~/.codex/config.toml（见 AGENTS.Codex.md）
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 严禁 stdout 被污染，日志走 stderr
def _log(msg: str) -> None:
    print(f"[sunzi-mcp] {msg}", file=sys.stderr, flush=True)


PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sunzi-investigation", "version": "1.0.0"}

# 被拒绝的操作者名（防止 agent 冒名顶替正兵）
_FORBIDDEN_OPERATORS = {"system", "ai", "assistant", "model", "bot", "auto", "llm"}


# ----------------------------------------------------------------------
# 会话权限上下文（REQ-009/011）：由 harness 经环境变量声明身份
#   SUNZI_OPERATOR 必填（缺省 → system 全旁路，向后兼容既有部署）
#   SUNZI_ROLE / SUNZI_CLEARANCE / SUNZI_NETWORK / SUNZI_PURPOSE 可选
# ----------------------------------------------------------------------
def _build_access():
    import os
    operator = os.environ.get("SUNZI_OPERATOR", "").strip()
    if not operator:
        from core.access import system_context
        return system_context()
    from core.access import AccessContext
    return AccessContext(
        operator=operator,
        role=os.environ.get("SUNZI_ROLE", "正兵"),
        clearance=int(os.environ.get("SUNZI_CLEARANCE", "1")),
        purpose=os.environ.get("SUNZI_PURPOSE", ""),
        network=os.environ.get("SUNZI_NETWORK", "local"),
    )


_ACCESS = None


def _access():
    global _ACCESS
    if _ACCESS is None:
        _ACCESS = _build_access()
    return _ACCESS


# ----------------------------------------------------------------------
# 工具定义（inputSchema 为 JSON Schema，字段必填性在这里声明）
# ----------------------------------------------------------------------
def _tools() -> list[dict]:
    return [
        {
            "name": "scan_anomaly",
            "description": (
                "虚实扫描：从银行流水/通话/轨迹中扫描异常，返回候选虚处。"
                "只标反常，不给定性；每条结果附溯源行。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["flow", "call", "trajectory", "all"],
                        "description": "扫描范围，默认 all",
                    },
                },
                "required": [],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "cross_jian",
            "description": (
                "五间交叉：统计因间/内间/反间/死间/生间的命中情况，"
                "给出交叉等级（单源=观察 / 双源=线索 / 三源以上=可立案依据候选）。"
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "graph_overpass",
            "description": (
                "图库两跳过桥识别：Cypher 多跳 + SQL 自连接双轨比对。"
                "未装 ladybug 时自动降级为 SQL 单轨并在 degraded 字段标注。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "max_hops": {
                        "type": "integer",
                        "description": "最大跳数，默认 2",
                        "minimum": 2,
                        "maximum": 4,
                    },
                },
                "required": [],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "clue_list",
            "description": (
                "线索列表：返回线索摘要（id / 标题 / 间类 / 优先级 / 状态）。"
                "只返回摘要，不返回 source_rows 明细，避免上下文膨胀。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["待查", "查证中", "已固证", "已排除", "已立案", "all"],
                        "description": "按状态过滤，默认 all",
                    },
                    "include_rows": {
                        "type": "boolean",
                        "description": "是否附带原始溯源行，默认 false（会显著增大返回）",
                    },
                },
                "required": [],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "clue_transition",
            "description": (
                "线索状态迁移。红线：operator 必须是具名正兵（拒绝 system/ai 等）；"
                "置『已立案』必须提供 legal_basis 且只能从『已固证』迁移。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "clue_id": {"type": "string", "description": "线索 ID"},
                    "to_status": {
                        "type": "string",
                        "enum": ["待查", "查证中", "已固证", "已排除", "已立案"],
                    },
                    "operator": {
                        "type": "string",
                        "description": "具名操作者，如『王检察官』；禁止 system/ai",
                    },
                    "note": {"type": "string", "description": "迁移说明"},
                    "legal_basis": {
                        "type": "string",
                        "description": "法定依据：仅置『已立案』时必填，如案号/审批文号",
                    },
                },
                "required": ["clue_id", "to_status", "operator"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "run_pipeline",
            "description": (
                "跑全链路管线。默认禁用（需 confirm=true 才执行），"
                "因为耗时长且会改写 output/ 与 DuckDB。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "必须为 true 才会真正执行",
                    },
                    "auto_review": {
                        "type": "boolean",
                        "description": "自动 accept 确认候选（仅演示，生产禁用）",
                    },
                },
                "required": ["confirm"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
        {
            "name": "function_list",
            "description": (
                "Ontology Function 目录：列出所有只读计算函数（名称/输入语义表/输出类型/"
                "参数/说明）。Function 只读不改对象，与可写的 clue_transition（Action）相对。"
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "function_invoke",
            "description": (
                "调用 Ontology Function（只读）：按 functions.json 声明执行 SQL/py 计算，"
                "返回 rows 或 report。SQL 实现强制 SELECT/WITH 白名单，禁止任何写操作。"
                "需先跑过管线（语义表 obj_*/lnk_* 已构建）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "函数名（见 function_list）"},
                    "params": {"type": "object", "description": "函数参数（可选）"},
                },
                "required": ["name"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "rule_list",
            "description": (
                "检测规则手册（只读）：列出自然语言检测规则（rule_text 判据原文、绑定 Function、"
                "参数阈值、维度/间类/假设挂钩）。编排约定：先读本手册理解判据，再按规则调 "
                "function_invoke（参数以规则 params 为基准），不得自创规则外判据、不写 SQL。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "description": "可选：按决策阶段过滤（xu_shi/qi_zheng/yong_jian）",
                    },
                },
                "required": [],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "review.list_pending",
            "description": (
                "人审工作台只读（REQ-021）：列出待正兵确认的实体对齐候选"
                "（candidate_id / entity_type / canonical / variants / reason / status，"
                "字段与 review_queue.json 一致）。不含证据明细；证据走 review.get_evidence。"
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "review.get_evidence",
            "description": (
                "人审工作台只读（REQ-021）：单条候选的证据引用、知识包/规则手册版本。"
                "正兵及以下角色只返回证据类型与计数摘要（受限），偏将及以上返回证据引用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "description": "候选 ID，如 rev_person_0001"},
                },
                "required": ["candidate_id"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "action.status",
            "description": (
                "两阶段动作只读（REQ-021）：按 action_id 查询提案状态机"
                "（proposed/approved/dispatching/pending_receipt/confirmed/failed/dead_letter）、"
                "审批人、尝试次数、外部业务号与回写发件箱记录。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_id": {"type": "string", "description": "动作提案 ID（act_ 前缀）"},
                },
                "required": ["action_id"],
            },
            "annotations": {"readOnlyHint": True},
        },
    ]


# ----------------------------------------------------------------------
# 内核桥接（惰性导入，避免启动失败）
# ----------------------------------------------------------------------
def _load_report() -> dict:
    """读取最近一次管线产物；没有则现场跑一遍轻量流程。"""
    p = ROOT / "output" / "lineage_clues.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    # 没有产物 → 现场生成（只跑侦查主流程，不跑全链路）
    from core import Store, get_registry, skill_invoke, lineage
    import skills.registry_bootstrap  # noqa: F401  注册五子技能

    store = Store()
    store.execute(
        "CREATE OR REPLACE TABLE 银行流水 AS "
        "SELECT * FROM read_parquet('data/银行流水.parquet')"
    )
    from core.hypotheses import MiaoSuan
    miao = MiaoSuan()
    miao.set_ji({
        "证据缺口": "现金来源无法溯源",
        "授权边界": "不可查房产车辆",
    })
    ctx = {
        "可用数据": ["银行流水", "通话记录", "招投标档案", "工商信息", "轨迹出行",
                     "公开OSINT", "举报材料"],
        "未调取": ["房产车辆"],
    }
    reg = get_registry()
    clues = []
    for sid in ["xu_shi", "qi_zheng", "yong_jian"]:
        clues.extend(skill_invoke(reg, sid, miao=miao, store=store, ctx=ctx))
    merged = lineage.dedupe_and_merge(clues, threshold=0.5)
    merged = lineage.prioritize_clues(merged)
    rep = lineage.lineage_report(merged)
    store.close()
    return rep


def _redline(obj: dict) -> dict:
    """给所有返回体强制挂红线字段。"""
    obj.setdefault("needs_human_review", True)
    obj["定性_policy"] = "AI 不给出定性，须言词证据+法定程序"
    obj["disclaimer"] = "候选线索，非结论；一切以原始证据与法定程序为准"
    return obj


def _build_board():
    """
    统一构造已恢复状态的处置看板。

    ⚠️ 关键：必须**先还原为 LineageClue 对象、再用 DuckDB 状态回填**。
    此前 clue_list 直接读 JSON（旧快照）、clue_transition 走 board.restore()
    读 DuckDB（真值源），两个工具看到的状态不一致——
    表现为"列表里显示查证中，迁移时却报已立案"。
    DuckDB 是持久化的真值源，JSON 只是产物快照，一律以 DuckDB 为准。
    """
    from core import Store
    from core.disposal import DisposalBoard
    from core.registry import LineageClue

    rep = _load_report()
    known = {f for f in LineageClue.__dataclass_fields__}
    clues = [
        LineageClue(**{k: v for k, v in c.items() if k in known})
        for c in rep.get("clues", [])
    ]
    store = Store()
    board = DisposalBoard(clues, store=store)
    board.restore()          # DuckDB → 内存，真值源回灌
    return board, store, rep


# ----------------------------------------------------------------------
# 工具实现
# ----------------------------------------------------------------------
def tool_scan_anomaly(args: dict) -> dict:
    scope = args.get("scope", "all")
    rep = _load_report()
    rows = []
    for c in rep.get("clues", []):
        if c.get("skill_id") != "xu_shi":
            continue
        s = scope
        hit = (s == "all") or (s == "flow" and "现金" in c.get("title", "")) \
            or (s == "call" and "通话" in c.get("title", "")) \
            or (s == "trajectory" and "轨迹" in c.get("title", ""))
        if hit:
            rows.append({
                "clue_id": c["clue_id"],
                "候选虚处": c.get("title"),
                "依据": (c.get("detail") or {}).get("依据"),
                "级别": (c.get("detail") or {}).get("级别"),
                "source_row_count": len(c.get("source_rows") or []),
                "溯源提示": "用 clue_list(include_rows=true) 取明细 ID，再回 DuckDB 原始表核对",
            })
    return _redline({"scope": scope, "count": len(rows), "findings": rows})


def tool_cross_jian(args: dict) -> dict:
    rep = _load_report()
    return _redline({
        "jian_coverage": {k: len(v) for k, v in rep.get("jian_coverage", {}).items()},
        "cross_level": rep.get("cross_level"),
        "total_clues": rep.get("total_clues"),
        "规则": "单源=观察 / 双源=线索 / 三源以上=可立案依据候选",
    })


def tool_graph_overpass(args: dict) -> dict:
    from core import Store
    from core.graph import GraphBackend, overpass_two_hop_sql, compare_engines

    store = Store()
    store.execute(
        "CREATE OR REPLACE TABLE 银行流水 AS "
        "SELECT * FROM read_parquet('data/银行流水.parquet')"
    )
    g = GraphBackend(str(ROOT / "data" / "ladybug" / "investigation.lbug"))
    if not g.available:
        paths = overpass_two_hop_sql(store)
        out = {
            "degraded": True,
            "reason": "未安装 ladybug（pip install ladybug），降级为 SQL 单轨",
            "engine": "sql",
            "paths": [p.to_dict() for p in paths],
        }
        store.close()
        return _redline(out)

    try:
        g.build_from_duckdb(store)
        cy = g.overpass_two_hop()
        sq = overpass_two_hop_sql(store)
        cmp_res = compare_engines(cy, sq)
        out = {"degraded": False, "engine": "cypher+sql", "comparison": cmp_res,
               "paths": [p.to_dict() for p in cy]}
    except Exception as e:
        paths = overpass_two_hop_sql(store)
        out = {"degraded": True, "reason": f"图库执行失败：{e}", "engine": "sql",
               "paths": [p.to_dict() for p in paths]}
    finally:
        g.close()
        store.close()
    return _redline(out)


def tool_clue_list(args: dict) -> dict:
    status = args.get("status", "all")
    include_rows = bool(args.get("include_rows", False))
    ctx = _access()
    board, store, rep = _build_board()
    try:
        # REQ-011 AC1：低权限会话只返回授权范围内线索——
        # 内间线索源自举报材料（tipoff，偏将及以上门槛），正兵及以下不可见
        from core.access import ROLE_RANK
        restricted = (not ctx.is_system
                      and ctx.rank < ROLE_RANK["偏将"])
        items = []
        filtered = 0
        for c in board.clues.values():
            if status != "all" and c.status != status:
                continue
            if restricted and "内间" in (c.jian_types or []):
                filtered += 1
                continue
            det = c.detail or {}
            item = {
                "clue_id": c.clue_id,
                "title": c.title,
                "skill_id": c.skill_id,
                "jian_types": c.jian_types,
                "assumption_chain": c.assumption_chain,
                "priority_rank": det.get("priority_rank"),
                "priority_score": det.get("priority_score"),
                "status": c.status,
                "source_row_count": len(c.source_rows or []),
            }
            if include_rows:
                item["source_rows"] = c.source_rows
            items.append(item)
        out = {
            "status_filter": status,
            "count": len(items),
            "clues": items,
            "状态源": "DuckDB（真值源），非 JSON 快照",
        }
        if restricted:
            out["access_note"] = (
                f"operator={ctx.operator} role={ctx.role}：按对象策略过滤内间线索"
                f" {filtered} 条（REQ-011 AC1）")
        return _redline(out)
    finally:
        store.close()


def tool_clue_transition(args: dict) -> dict:
    """状态迁移。红线在工具层强制，不依赖调用方自觉。"""
    operator = str(args.get("operator", "")).strip()
    to_status = args.get("to_status")
    legal_basis = args.get("legal_basis")

    # 红线 1：操作者必须具名
    if not operator or operator.lower() in _FORBIDDEN_OPERATORS:
        return _redline({
            "ok": False,
            "error": f"operator 必须是具名正兵，拒绝 {operator!r}。"
                     f"禁止使用 {sorted(_FORBIDDEN_OPERATORS)} 等占位名",
        })

    # REQ-009：写操作主体一致性 + human 终态角色门槛
    ctx = _access()
    if not ctx.is_system:
        if operator != ctx.operator:
            return _redline({
                "ok": False,
                "error": f"会话主体 {ctx.operator!r}(role={ctx.role}) 不得以"
                         f" {operator!r} 名义执行写动作（REQ-009 主体一致性）",
            })
        from core.access import HUMAN_ONLY_STATUSES
        if to_status in HUMAN_ONLY_STATUSES and ctx.role != "human":
            return _redline({
                "ok": False,
                "error": f"role={ctx.role} 无权迁移到 {to_status!r}"
                         f"（human 专属终态，REQ-009）",
            })

    # 红线 2：立案必须带法定依据
    if to_status == "已立案" and not legal_basis:
        return _redline({
            "ok": False,
            "error": "置『已立案』必须提供 legal_basis（案号/审批文号）",
        })

    board, store, _rep = _build_board()
    if not board.clues:
        store.close()
        return _redline({"ok": False, "error": "无线索，请先跑管线"})

    try:
        if to_status == "已立案":
            # 立案只能从"已固证"迁移；终态不可改，需先确认当前状态再决定路径。
            cur = board.clues[args["clue_id"]].status
            if cur == "已立案":
                return _redline({"ok": False, "current_status": cur,
                                 "error": "该线索已立案，不可重复立案"})
            if cur != "已固证":
                board.transition(args["clue_id"], "已固证", operator=operator,
                                 note=args.get("note", ""))
            board.file(args["clue_id"], operator=operator, legal_basis=legal_basis)
        else:
            board.transition(args["clue_id"], to_status, operator=operator,
                             note=args.get("note", ""))
        board.persist()
        return _redline({
            "ok": True,
            "clue_id": args["clue_id"],
            "to_status": to_status,
            "operator": operator,
            "legal_basis": legal_basis,
            "audit_tail": (board.clues[args["clue_id"]].audit_log or [])[-3:],
        })
    except (ValueError, KeyError) as e:
        return _redline({"ok": False, "error": str(e)})
    finally:
        store.close()


def tool_run_pipeline(args: dict) -> dict:
    if not args.get("confirm"):
        return _redline({"ok": False, "error": "需 confirm=true 才会执行"})
    import subprocess
    cmd = [sys.executable, "run_all.py", "--no-cli"]
    if args.get("auto_review"):
        cmd.append("--auto-review")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          timeout=600)
    tail = (proc.stdout or "")[-2000:]
    return _redline({
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": tail,
        "stderr_tail": (proc.stderr or "")[-1000:],
    })


def tool_function_list(args: dict) -> dict:
    """Ontology Function 目录（只读声明，不碰数据）。"""
    from core.functions import FunctionExecutor

    try:
        catalog = FunctionExecutor(store=None).catalog()
    except Exception as e:
        return _redline({"ok": False, "error": f"Function 目录装载失败：{e}"})
    return _redline({"count": len(catalog), "functions": catalog})


def tool_function_invoke(args: dict) -> dict:
    """调用只读 Function。语义表缺失时提示先跑管线。"""
    from core import Store
    from core.functions import FunctionExecutor

    name = args.get("name")
    if not name:
        return _redline({"ok": False, "error": "必填参数 name"})
    store = Store()
    try:
        r = FunctionExecutor(store, access=_access()).invoke(name, args.get("params") or {})
        return _redline({"ok": True, **r})
    except KeyError as e:
        return _redline({"ok": False, "error": str(e)})
    except Exception as e:
        return _redline({
            "ok": False, "error": str(e),
            "提示": "若报语义表不存在，先跑 run_pipeline(confirm=true) 构建 obj_*/lnk_*",
        })
    finally:
        store.close()


def tool_rule_list(args: dict) -> dict:
    """检测规则手册（自然语言规则目录，只读声明，不碰数据）。"""
    from core.rules import catalog

    try:
        rules = catalog()
    except Exception as e:
        return _redline({"ok": False, "error": f"规则手册装载失败：{e}"})
    stage = args.get("stage")
    if stage:
        rules = [r for r in rules if r.get("stage") == stage]
    return _redline({"count": len(rules), "rules": rules, "readonly": True})


# ----------------------------------------------------------------------
# REQ-021（读轨）：人审工作台 / 两阶段动作 的只读可见性
#   submit_proposal 等写动作属 REQ-021 写轨，不在本批开放；
#   Agent 不得直接调 clue_transition 写状态机之外的审查对象。
# ----------------------------------------------------------------------
def _load_review_queue():
    """装载 output/review_queue.json；未生成时返回 (None, 提示)。"""
    from core.review import ReviewQueue
    path = ROOT / "output" / "review_queue.json"
    if not path.exists():
        return None, "review_queue.json 未生成（先跑 run_pipeline）"
    return ReviewQueue.load(str(path)), None


def tool_review_list_pending(args: dict) -> dict:
    """待确认候选列表（字段与 review_queue.json 一致，evidence 不铺明细）。"""
    q, note = _load_review_queue()
    if q is None:
        return _redline({"count": 0, "pending": [], "note": note, "readonly": True})
    pending = []
    for d in q.pending():
        item = d.to_dict()
        item.pop("evidence", None)      # 列表不附证据明细，按需走 get_evidence
        pending.append(item)
    return _redline({
        "count": len(pending), "pending": pending, "summary": q.summary(),
        "字段源": "output/review_queue.json", "readonly": True,
    })


def tool_review_get_evidence(args: dict) -> dict:
    """单条候选的证据引用 + 知识/规则版本；受限角色只给摘要计数。"""
    from core.access import ROLE_RANK
    candidate_id = args.get("candidate_id")
    if not candidate_id:
        return _redline({"ok": False, "error": "必填参数 candidate_id"})
    q, note = _load_review_queue()
    if q is None:
        return _redline({"ok": False, "error": note})
    try:
        d = q.get(candidate_id)
    except KeyError as e:
        return _redline({"ok": False, "error": str(e)})

    ctx = _access()
    evidence = d.evidence or {}
    restricted = not ctx.is_system and ctx.rank < ROLE_RANK["偏将"]
    out = {
        "ok": True, "readonly": True,
        "candidate_id": d.candidate_id,
        "entity_type": d.entity_type,
        "canonical": d.canonical,
        "variants": d.variants,
        "status": d.status,
        "reason": d.reason,
    }
    # 版本溯源：知识包版本（R5 等）+ 规则手册条数
    try:
        from core.functions import load_case_knowledge
        out["knowledge_version"] = load_case_knowledge().get("knowledge_version")
    except Exception:
        out["knowledge_version"] = None
    out["rulebook_size"] = None
    try:
        from core.rules import catalog
        out["rulebook_size"] = len(catalog())
    except Exception:
        pass

    if restricted:
        # 正兵及以下：只给证据类型与计数（受限摘要），不铺原始引用
        out["evidence_summary"] = {
            k: (len(v) if isinstance(v, (list, dict, str)) else v)
            for k, v in evidence.items()
        }
        out["access_note"] = (
            f"operator={ctx.operator} role={ctx.role}：证据明细限偏将及以上，"
            "仅返回类型/计数摘要（REQ-021 受限摘要）")
        out["evidence_refs"] = []
    else:
        refs = []
        for k, v in evidence.items():
            if isinstance(v, list):
                refs.extend(str(x) for x in v[:50])
            else:
                refs.append(f"{k}={v}")
        out["evidence_refs"] = refs
    return _redline(out)


def tool_action_status(args: dict) -> dict:
    """两阶段动作查询：提案状态 / 审批 / 回执（只读）。"""
    from core import Store
    from core.action_executor import ActionExecutor, ActionRequestNotFound

    action_id = args.get("action_id")
    if not action_id:
        return _redline({"ok": False, "error": "必填参数 action_id"})
    store = Store()
    try:
        ex = ActionExecutor(store, access=_access())
        try:
            req = ex.request_status(action_id)
        except ActionRequestNotFound:
            return _redline({"ok": False, "error": f"动作提案不存在：{action_id}"})
        out = {"ok": True, "readonly": True, "action": req}
        # 回写发件箱/外部回执（若已进入回写轨）
        try:
            rows = store.conn.execute(
                "SELECT outbox_id, status, attempts, external_id, last_error, "
                "idempotency_key, created_at, sent_at, confirmed_at "
                "FROM writeback_outbox WHERE action_id=?", [action_id]).fetchall()
            cols = ["outbox_id", "status", "attempts", "external_id", "last_error",
                    "idempotency_key", "created_at", "sent_at", "confirmed_at"]
            out["writeback"] = [dict(zip(cols, r)) for r in rows]
        except Exception:
            out["writeback"] = []
        return _redline(out)
    finally:
        store.close()


_TOOL_IMPL: dict[str, Callable[[dict], dict]] = {
    "scan_anomaly": tool_scan_anomaly,
    "cross_jian": tool_cross_jian,
    "graph_overpass": tool_graph_overpass,
    "clue_list": tool_clue_list,
    "clue_transition": tool_clue_transition,
    "run_pipeline": tool_run_pipeline,
    "function_list": tool_function_list,
    "function_invoke": tool_function_invoke,
    "rule_list": tool_rule_list,
    "review.list_pending": tool_review_list_pending,
    "review.get_evidence": tool_review_get_evidence,
    "action.status": tool_action_status,
}


# ----------------------------------------------------------------------
# JSON-RPC 层
# ----------------------------------------------------------------------
def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _handle(msg: dict) -> dict | None:
    """处理单条消息。通知（无 id）返回 None 表示不回响应。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request: not an object")

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "孙武侦查官：确定性侦查推演内核。"
                "禁止自行编写业务 SQL，禁止把原始明细搬进上下文，"
                "禁止给出定性结论或置『已立案』（需具名正兵+法定依据）。"
            ),
        })

    if method == "notifications/initialized":
        return None  # 通知，不回响应

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": _tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _TOOL_IMPL:
            return _err(req_id, -32601, f"Method not found: {name}")
        try:
            data = _TOOL_IMPL[name](arguments)
            return _ok(req_id, {
                "content": [{"type": "text",
                             "text": json.dumps(data, ensure_ascii=False, indent=2,
                                                default=str)}],
                "isError": False,
            })
        except KeyError as e:
            return _err(req_id, -32602, f"Invalid params: {e}")
        except Exception as e:  # 工具内部错误也要回 JSON-RPC，不能崩进程
            _log(traceback.format_exc())
            return _err(req_id, -32603, f"Internal error: {type(e).__name__}: {e}")

    if method in ("resources/list", "prompts/list"):
        return _ok(req_id, {"resources": [], "prompts": []})

    return _err(req_id, -32601, f"Method not found: {method}")


def main() -> int:
    _log(f"starting, protocol={PROTOCOL_VERSION}, root={ROOT}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps(_err(None, -32700, f"Parse error: {e}"),
                                        ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
