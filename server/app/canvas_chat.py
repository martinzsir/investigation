"""
server/app/canvas_chat.py
RC-301 画布只读问答（RC-302 引用校验串联）。

流程：
  1. 从画布文档构建业务视图上下文（规则判据/事实/假设/核实项/书证/行摘要）；
  2. redact_payload 脱敏 + redaction_hash（call_llm 闸门验真）；
  3. 走 call_llm 闸门（llm_enabled / network / model 白名单 / 脱敏双保险）；
  4. LLM 输出过 citation_guard：有据句入 facts、无据句入 pending、假引用剔除；
  5. 返回 {answer, facts, pending, warnings, citations}；不写画布（只读）。

双模式开关（canvas_chat_mode）：
  - direct（默认）：LLMClient.chat 直接调用，单轮问答；
  - react：AgentScope ReAct 智能助手，可调用只读 MCP 工具（rule_list /
    clue_list / function_invoke / report.gather_evidence / report.render 等）
    多轮推理后给出带引用的回答。ReAct 模式仍走 call_llm 闸门（fake_invoke
    注入点），复用网络/模型白名单/脱敏/日志全套检查；MCP 工具只注册只读
    白名单（纵深防御 + 服务端 access 控制）；AgentScope 懒导入，缺失时
    返回业务错误（react_unavailable），不崩。

纪律：
  - 问答不产生任何画布写入（只读断言，RC-301 AC4）；
  - 发送给模型的上下文不含 denied 字段明文（复用 redact_payload）；
  - llm_enabled=false 或 network=isolated 时由 call_llm 闸门拒绝，返回业务错误；
  - 回答引用角标全部能在当刻文档中解析到节点/行；解析不到的引用被剔除。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from core.access import AccessContext, LLMBlockedError
from core.llm.draft_verify import select_deployment, _deployment_policy
from core.llm.llm_client import LLMClient
from core.llm.redact import (
    call_llm,
    call_llm_stream,
    load_llm_policy,
    redact_payload,
)

from server.app.canvas_citation_guard import (
    build_valid_refs_from_doc,
    validate_citations,
)

# 引用标记：LLM 输出中事实句用 [cite:<ref>] 锚定证据
CITE_MARK = "[cite:{}]"

# 项目根（MCP server 子进程 cwd）
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)

# ----------------------------------------------------------------------
# 系统提示词：direct / react 由同一 builder 构建（约束单一来源，防漂移）
# ----------------------------------------------------------------------

# 无据句准入与禁止输出清单（两模式共用）。
# 背景：无引用句会被 citation_guard 整条收进「待核实」，分析师可一键
# 转为核查任务，因此每一句都必须是可核查的事实命题，元话语/标签/
# UI 话术混入即成为截图中那种垃圾碎片。
_PENDING_RULES = (
    "无据句的准入（系统会自动把无引用句收集为「待核实」，每一句都可能"
    "被分析师一键转为核查任务，请逐句负责）：\n"
    "只有同时满足以下三条，才可作为无据句输出：\n"
    "(1) 关于案件事实的单一命题（人/事/时/地/物/资金/关系）；\n"
    "(2) 将来可通过调取书证或数据被证实或证伪；\n"
    "(3) 脱离上下文也能独立读懂（主语、对象、时间范围齐全）。\n"
    "正例（仅示意写法，非本案事实）：海州建材 2024 年 3 月基本户可能"
    "无对应经营性进项。\n"
    "以下内容一律禁止输出：\n"
    "- 分段标题或标签，如「事实性陈述：」「模型推测：」「结论」；\n"
    "- 对自身能力或数据范围的说明，如「画布未加载…」「无法判断…」"
    "「缺少…字段」；\n"
    "- 流程或界面话术，如「提交为核实建议」「建议后续调取」"
    "「以上仅供参考」；\n"
    "- 反问、礼貌套话，以及对问题或操作过程的复述。\n"
    "证据确实不足以回答时：只用一句平实的话指出缺哪类证据（不超过 50 "
    "字，不含「建议」「提交」字样），除此之外不再输出任何无据句。"
)

# 只读工具纪律（仅 react 模式）
_TOOL_RULES = (
    "一、工具纪律\n"
    "1. 工具只用于查证，结论必须以工具返回为依据；不得自创规则外判据，"
    "不得写 SQL；你没有任何写工具，不得输出状态变更指令或写操作建议。\n"
    "2. 工具返回中 needs_human_review=true 表示内容需人审，不得当作"
    "已确认事实陈述。\n"
    "3. 调用 report.render 时只传 case_id + sections（先调 "
    "report.gather_evidence，evidence 自动取缓存），不要传递整个 "
    "evidence 对象。\n"
    "\n"
)


def _build_system_prompt(react: bool) -> str:
    """构建系统提示词。

    react=True 时前置只读工具纪律，并收紧 ref 来源口径：工具返回的
    溯源 ID 仅当与画布「可用引用」清单中某个 ref 逐字一致时才可引用
    —— RC-302 白名单只来自当刻画布文档，对不上的 ID 会被判假引用，
    故 prompt 不得承诺工具 ID 可直接引用。
    """
    if react:
        role = (
            "你是线索研判助手，可调用只读工具（rule_list / clue_list / "
            "function_invoke / report.gather_evidence / report.render 等）"
            "查证，并结合当前画布业务视图回答分析师的问题。"
        )
        ref_source = (
            "ref 只能逐字复制画布「可用引用」清单中的值；工具返回的溯源 "
            "ID 仅当与清单中某个 ref 逐字一致时才可引用，不一致一律不用"
        )
        cite_no, pending_no, redline_no = "二", "三", "四"
        head = _TOOL_RULES
    else:
        role = (
            "你是线索研判助手，只依据当前画布业务视图（规则判据、事实、"
            "假设、核实项状态、书证、数据行摘要）回答分析师的问题。"
        )
        ref_source = "ref 只能逐字复制下方「可用引用」清单中的值"
        cite_no, pending_no, redline_no = "一", "二", "三"
        head = ""

    return (
        f"{role}\n"
        "\n"
        f"{head}"
        f"{cite_no}、引用（事实句的唯一凭证）\n"
        f"1. 每条事实性陈述必须在句末紧跟 [cite:<ref>]，{ref_source}；"
        "一句多证写 [cite:a][cite:b]，清单中没有的值一律不得编造。\n"
        "2. [cite:<ref>] 是唯一合法的引用写法；禁止 ref:xxx、"
        "@local#row/*、括号注明等任何其它形式。\n"
        "3. 拿不出合法引用的句子，不得写成事实。\n"
        "\n"
        f"{pending_no}、{_PENDING_RULES}\n"
        "\n"
        f"{redline_no}、红线\n"
        "1. 不输出任何状态变更指令（立案/核实/固证）或写操作建议。\n"
        "2. 回答用中文，直接输出正文，不要标题、引言和结语。"
    )


SYSTEM_PROMPT = _build_system_prompt(react=False)
REACT_SYSTEM_PROMPT = _build_system_prompt(react=True)

# ReAct 模式：只读工具白名单（client 侧纵深防御，服务端 access 控制兜底）
# 9 个只读侦查工具 + 2 个 sunzi-report 桥接工具
REACT_TOOL_WHITELIST = [
    "scan_anomaly", "cross_jian", "graph_overpass", "clue_list",
    "function_list", "function_invoke", "rule_list",
    "review.list_pending", "review.get_evidence",
    "report.gather_evidence", "report.render",
]

# MCP 工具名经 function-name 规范化（OpenAI/DashScope 仅允许 [A-Za-z0-9_-]），
# "." 被替换成 "x"（如 report.gather_evidence → reportxgather_evidence）。
# 反向映射仅用于 SSE 事件的前端友好展示，不影响实际工具路由。
_TOOL_DISPLAY_NAMES: dict[str, str] = {
    n.replace(".", "x"): n for n in REACT_TOOL_WHITELIST
}


def _short_tool_name(raw: str) -> str:
    """mcp__<client>__<name> → <name>，并还原被规范化的点号。"""
    short = raw.split("__")[-1] if "__" in raw else raw
    return _TOOL_DISPLAY_NAMES.get(short, short)


# 可作为 artifact 直通的工具：产物可信度来自确定性管道（内核采集 + 模板
# 渲染），而非 LLM 逐句陈述，故不适用逐句 [cite:] 分流——报告 md 自带
# 「待核实事项」段与降级声明，其完整性由工具保证，LLM 只撰写叙述段。
_ARTIFACT_TOOLS = {"report.render"}


def _extract_tool_artifact(tool_name: str, result_text: str) -> dict | None:
    """从工具结果文本解析确定性产物（当前仅 report.render 的 md）。

    MCP tools/call 的 text content 即工具返回 dict 的 JSON 序列化
    （scripts/mcp_server.py 用 indent=2 的 json.dumps 打包）；
    解析失败 / ok=false / 无 md → None（回落普通逐句引用校验）。
    """
    if tool_name not in _ARTIFACT_TOOLS or not result_text:
        return None
    try:
        payload = json.loads(result_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    md = payload.get("md")
    if not isinstance(md, str) or not md.strip():
        return None
    return {
        "artifact_type": "sunzi-report",
        "tool": tool_name,
        "format": str(payload.get("format") or "md"),
        "report_type": str(payload.get("type") or "A"),
        "type_name": str(payload.get("type_name") or "研判报告"),
        "text": md,
        "degraded": bool(payload.get("降级声明")),
    }

# REACT_SYSTEM_PROMPT 由上方 _build_system_prompt(react=True) 统一构建


def build_canvas_context(doc: dict[str, Any]) -> dict[str, Any]:
    """从画布文档构建业务视图上下文（脱敏前的结构化载荷）。

    只含业务文本：规则判据、事实、假设、核实项状态、书证、行摘要；
    不含原始数据行明细值（行只给 ref + label 摘要）。
    """
    nodes = doc.get("nodes", []) or []
    rules = []
    facts = []
    hypotheses = []
    verify_items = []
    evidences = []
    rows = []

    for n in nodes:
        ref = str(n.get("ref", ""))
        kind = n.get("kind", "")
        label = n.get("label", "")
        props = n.get("props") or {}

        if kind == "rule":
            rules.append({
                "ref": ref,
                "判据": props.get("rule_text") or label,
                "依据": props.get("依据", ""),
            })
        elif kind == "fact":
            facts.append({"ref": ref, "事实": label or props.get("text", "")})
        elif kind == "hypothesis":
            hypotheses.append({
                "ref": ref,
                "标题": props.get("title", label),
                "内容": props.get("content", ""),
            })
        elif kind == "verify_item":
            verify_items.append({
                "ref": ref,
                "核实项": label,
                "状态": props.get("status", "建议"),
            })
        elif kind == "evidence":
            evidences.append({"ref": ref, "书证": label})
        elif kind == "source_row":
            rows.append({"ref": ref, "摘要": label})

    return {
        "规则": rules,
        "事实": facts,
        "假设": hypotheses,
        "待核实": verify_items,
        "书证": evidences,
        "数据行摘要": rows,
        "可用引用": sorted(build_valid_refs_from_doc(doc)),
    }


def _make_user_prompt(question: str, context: dict[str, Any]) -> str:
    return json.dumps({
        "画布业务视图": context,
        "分析师问题": question,
        "输出要求": (
            "直接输出回答正文，不要写「事实性陈述」「模型推测」等任何"
            "标题或标签（系统会按引用自动分流，无需分段）；事实句逐句在"
            "句末挂 [cite:<ref>]，ref 只能取自「可用引用」清单；其余内容"
            "只允许是可独立核查的单一事实命题；数据不足时用一句话指出"
            "缺什么证据，不写行动建议或界面话术。"
        ),
    }, ensure_ascii=False, indent=2)


def _make_react_user_prompt(question: str, context: dict[str, Any],
                            case_id: str, clue_id: str) -> str:
    """ReAct 模式的 user prompt（含 case/clue 上下文供工具调用参照）。"""
    return json.dumps({
        "画布业务视图": context,
        "案件": case_id,
        "线索": clue_id,
        "分析师问题": question,
        "输出要求": (
            "先调用只读工具查证，再直接输出回答正文；不要写任何标题或"
            "标签（系统会按引用自动分流，无需分段）；事实句逐句在句末挂 "
            "[cite:<ref>]，ref 只能取自「可用引用」清单（工具返回的溯源 "
            "ID 仅当与清单中某个 ref 逐字一致时才可使用）；其余内容只允许"
            "是可独立核查的单一事实命题；数据不足时用一句话指出缺什么"
            "证据，不写行动建议或界面话术。"
        ),
    }, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# ReAct 模式：AgentScope 懒导入 + MCP 子进程 + ReAct 智能助手
# ----------------------------------------------------------------------

def _build_react_model(dep: dict, model_name: str, *, stream: bool = False):
    """从部署档构建 AgentScope 模型实例。

    支持 DashScope（cloud 档）和 OpenAI 兼容接口（local 档 / Ollama）。
    AgentScope 懒导入：缺失时抛 ImportError（上层捕获后返回业务错误）。

    ReAct 模式下 qwen-plus 偶发生成不合法 function.arguments（DashScope
    后端自校验失败返回 400 InternalError.Algo），model 层级默认重试 4 次
    用同样请求仍可能失败。这里提高 max_retries=5（共 6 次尝试）+
    retry_delay=2.0s，给偶发错误更多恢复窗口。

    stream=True 时 LLM 真正逐 token 流式（用于 SSE 端点，让思考过程/文本
    增量即时推给前端）；stream=False 时一次性返回（用于 /chat 端点）。
    """
    from agentscope.model import DashScopeChatModel, OpenAIChatModel
    from agentscope.credential import DashScopeCredential, OpenAICredential

    base_url = dep.get("base_url", "")
    ds_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()

    # ReAct 偶发 400 重试参数（DashScope 后端算法层偶发生成不合法
    # function.arguments；同样请求重试有概率成功）
    react_retries = 5
    react_retry_delay = 2.0

    if "dashscope" in base_url or "aliyuncs" in base_url:
        return DashScopeChatModel(
            credential=DashScopeCredential(api_key=ds_key),
            model=model_name,
            stream=stream,
            max_retries=react_retries,
            retry_delay=react_retry_delay,
        )
    # OpenAI 兼容接口（Ollama / 其它 local 档）
    return OpenAIChatModel(
        credential=OpenAICredential(
            api_key=ds_key or "dummy",
            base_url=base_url or None,
        ),
        model=model_name,
        stream=stream,
        max_retries=react_retries,
        retry_delay=react_retry_delay,
    )


def _build_mcp_client(ctx: AccessContext):
    """构建 AgentScope MCP client（stdio 连接 scripts.mcp_server）。

    会话身份沿用当前 ctx（operator/role/clearance/network/purpose）；
    enable_tools 只注册只读白名单（纵深防御 + 服务端 access 兜底）。
    """
    from agentscope.mcp import MCPClient, StdioMCPConfig

    env = dict(os.environ)
    env.update({
        "SUNZI_OPERATOR": ctx.operator,
        "SUNZI_ROLE": ctx.role,
        "SUNZI_CLEARANCE": str(ctx.clearance),
        "SUNZI_NETWORK": ctx.network,
        "SUNZI_PURPOSE": ctx.purpose or "画布问答 ReAct",
    })

    return MCPClient(
        name="canvas-chat-react",
        is_stateful=True,  # stdio 传输必须 stateful（AgentScope 2.0.8 校验）
        mcp_config=StdioMCPConfig(
            command=sys.executable,
            args=["-m", "scripts.mcp_server"],
            env=env,
            cwd=_PROJECT_ROOT,
        ),
        enable_tools=REACT_TOOL_WHITELIST,
        execution_timeout=90,
    )


# 画布 ReAct 专用技能目录（server/react_skills/<name>/SKILL.md）。
# 独立于 .trae/skills（TRAE harness 发现区），只服务运行时 ReAct agent。
_REACT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "react_skills"


def _build_react_toolkit():
    """构建注册了画布专用技能的 Toolkit（AgentScope 懒导入）。

    LocalSkillLoader 扫描 SKILL.md（按 mtime 缓存）；注册后 Agent 每轮自动
    在 system prompt 末尾收到 <agent-skills> 清单（仅 name/description），
    并获得框架内置只读工具 Skill 按需读取技能正文（渐进式披露）。
    Skill 为内置非 MCP 工具，不受 REACT_TOOL_WHITELIST 过滤。
    """
    from agentscope.skill import LocalSkillLoader
    from agentscope.tool import Toolkit

    return Toolkit(skills_or_loaders=[
        LocalSkillLoader(directory=str(_REACT_SKILLS_DIR), scan_subdir=True),
    ])


async def _run_react_agent_async(
    *, user_prompt: str, dep: dict, model_name: str,
    ctx: AccessContext,
) -> dict:
    """构建并运行 AgentScope ReAct 智能助手（非流式 /chat 端点用）。

    返回 {"text": 最终回答文本, "artifact": 确定性产物 dict | None}。
    复用流式桥接器（stream=True 模型），丢弃思考/增量事件，仅汇总
    done 事件——与 SSE 路径保持单一实现，避免双份 Agent 装配漂移。

    AgentScope 懒导入：缺失时抛 ImportError（上层捕获返回
    react_unavailable）；MCP 子进程在生成器 finally 中关闭。
    """
    text = ""
    artifact = None
    async for evt in _run_react_agent_stream(
            user_prompt=user_prompt, dep=dep,
            model_name=model_name, ctx=ctx):
        evt_type = evt.get("type")
        if evt_type == "done":
            text = evt.get("text") or text
            artifact = evt.get("artifact")
        elif evt_type == "error":
            raise RuntimeError(evt.get("message", "ReAct 执行失败"))
    return {"text": text, "artifact": artifact}


# ----------------------------------------------------------------------
# ReAct 流式：async 事件生成器 → 桥接到 sync SSE 生成器
# ----------------------------------------------------------------------

async def _run_react_agent_stream(
    *, user_prompt: str, dep: dict, model_name: str,
    ctx: AccessContext,
):
    """ReAct 智能助手事件流（async 生成器，产出统一格式 dict 事件）。

    事件类型（dict）：
      {"type": "thinking", "text"}        思考过程增量
      {"type": "delta", "text"}           最终答案文本增量
      {"type": "tool_call", "name", "id"} 工具调用开始
      {"type": "tool_result", "name", "id", "state"} 工具调用结束
      {"type": "done", "text"}            ReAct 完成，带最终答案
      {"type": "error", "message"}        错误（含 react_unavailable）

    红线：
      - 懒导入 AgentScope，缺失时产出 error 事件（上层转 SSE error）
      - MCP 子进程在 finally 中关闭（防泄漏）
      - LLM stream=True，让思考/文本真正逐 token 流出
    """
    try:
        from agentscope.agent import Agent, ReActConfig
        from agentscope.agent._config import ModelConfig
        from agentscope.event import (
            TextBlockDeltaEvent,
            ThinkingBlockDeltaEvent,
            ToolCallStartEvent,
            ToolResultStartEvent,
            ToolResultTextDeltaEvent,
            ToolResultEndEvent,
        )
        from agentscope.message import Msg, TextBlock
    except ImportError as e:
        yield {"type": "error",
               "message": f"AgentScope 未安装，react 模式不可用：{e}"}
        return

    # stream=True 让 LLM 真正逐 token 流式
    model = _build_react_model(dep, model_name, stream=True)
    client = _build_mcp_client(ctx)
    await client.connect()
    final_text = ""
    # id -> short_name 映射（ToolResultEndEvent 没有 name，需用 id 关联）
    tool_name_map: dict[str, str] = {}
    # id -> 工具结果文本分片（ToolResultTextDeltaEvent 累积，
    # 用于在 done 前提取 report.render 的确定性 md 产物）
    tool_result_texts: dict[str, list[str]] = {}
    # 已锁定的确定性产物：report.render 一旦成功即提取（不等循环结束）。
    # DashScope qwen-plus 在 render 之后的收尾轮偶发 400（畸形
    # function.arguments），产物在手时不应被后续闲聊轮的模型错误拖死——
    # 循环异常则以产物降级出 done。
    secured_artifact: dict | None = None
    try:
        toolkit = _build_react_toolkit()
        for t in await client.list_tools():
            await toolkit.add_tool(t)

        agent = Agent(
            name="canvas-chat-assistant",
            system_prompt=REACT_SYSTEM_PROMPT,
            model=model,
            toolkit=toolkit,
            react_config=ReActConfig(max_iters=10),
            model_config=ModelConfig(max_retries=1),
        )

        msg = Msg(name="侦查员", role="user",
                  content=[TextBlock(type="text", text=user_prompt)])

        # yield_final_msg=True：最后会 yield 最终 AssistantMsg，从中拿完整文本
        async for evt in agent.reply_stream(msg, yield_final_msg=True):
            # 思考增量（qwen-plus 默认带 reasoning_content）
            if isinstance(evt, ThinkingBlockDeltaEvent):
                if evt.delta:
                    yield {"type": "thinking", "text": evt.delta}
            # 最终答案文本增量
            elif isinstance(evt, TextBlockDeltaEvent):
                if evt.delta:
                    final_text += evt.delta
                    yield {"type": "delta", "text": evt.delta}
            # 工具调用开始
            elif isinstance(evt, ToolCallStartEvent):
                # 去掉 mcp__<client>__ 前缀 + 还原点号，前端友好显示
                short_name = _short_tool_name(evt.tool_call_name)
                tool_name_map[evt.tool_call_id] = short_name
                yield {"type": "tool_call", "name": short_name,
                       "id": evt.tool_call_id}
            # 工具结果开始（带 name，EndEvent 没有 name）
            elif isinstance(evt, ToolResultStartEvent):
                short_name = _short_tool_name(evt.tool_call_name)
                tool_name_map[evt.tool_call_id] = short_name
                yield {"type": "tool_result", "name": short_name,
                       "id": evt.tool_call_id, "state": "running"}
            # 工具结果文本分片（MCP 结果即 JSON 文本，累积后供 artifact 解析）
            elif isinstance(evt, ToolResultTextDeltaEvent):
                if evt.delta:
                    tool_result_texts.setdefault(
                        evt.tool_call_id, []).append(evt.delta)
            # 工具结果结束（用 id 关联到 name，仅更新 state）
            elif isinstance(evt, ToolResultEndEvent):
                name = tool_name_map.get(evt.tool_call_id, "unknown")
                if os.environ.get("SUNZI_REACT_DEBUG"):
                    raw_db = "".join(tool_result_texts.get(
                        evt.tool_call_id, []))
                    print(f"[react-diag] tool_end name={name} "
                          f"state={evt.state} raw_len={len(raw_db)}",
                          file=sys.stderr, flush=True)
                    if name == "report.render":
                        try:
                            _p = json.loads(raw_db)
                            print(f"[react-diag] render parsed ok="
                                  f"{_p.get('ok')} "
                                  f"error={str(_p.get('error'))[:200]} "
                                  f"md_len={len(_p.get('md') or '')}",
                                  file=sys.stderr, flush=True)
                        except Exception as _e:
                            print(f"[react-diag] render raw not JSON: "
                                  f"{type(_e).__name__}: {_e}; "
                                  f"head={raw_db[:300]!r}",
                                  file=sys.stderr, flush=True)
                # 成功渲染即刻锁定产物（多次 render 以最近成功为准）
                if name == "report.render":
                    _art = _extract_tool_artifact(
                        "report.render",
                        "".join(tool_result_texts.get(evt.tool_call_id, [])))
                    if _art is not None:
                        secured_artifact = _art
                yield {"type": "tool_result", "name": name,
                       "id": evt.tool_call_id,
                       "state": str(evt.state)}
            # 最终 Msg（yield_final_msg=True 触发）
            elif hasattr(evt, "get_text_content") and callable(
                    evt.get_text_content):
                # AssistantMsg 实例：拿最终完整文本
                text = evt.get_text_content() or ""
                if text:
                    final_text = text

        # 确定性产物直通：优先用工具结果到达时即锁定的产物；兜底再反向
        # 扫描最后一次成功的 report.render（防御事件顺序差异）。
        artifact = secured_artifact
        if artifact is None:
            for call_id in reversed(list(tool_name_map.keys())):
                if tool_name_map.get(call_id) != "report.render":
                    continue
                raw = "".join(tool_result_texts.get(call_id, []))
                artifact = _extract_tool_artifact("report.render", raw)
                if artifact is not None:
                    break
        if os.environ.get("SUNZI_REACT_DEBUG"):
            print(f"[react-diag] artifact_extracted={artifact is not None} "
                  f"final_text_len={len(final_text)}",
                  file=sys.stderr, flush=True)

        # 流结束，发 done 事件（含最终答案；artifact 非空时由上层以 md
        # 原文作为答复正文，绕过逐句引用分流）
        yield {"type": "done", "text": final_text, "artifact": artifact}
    except Exception as e:
        # 确定性产物已到手时，后续轮次（如 render 后的确认语）模型偶发
        # 报错不拖垮交付：以产物降级出 done，正文用固定确认语。
        if secured_artifact is not None:
            if os.environ.get("SUNZI_REACT_DEBUG"):
                print(f"[react-diag] degrade to artifact after error: "
                      f"{type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            fallback_text = (final_text.strip()
                             or "已生成研判报告，见下。")
            yield {"type": "done", "text": fallback_text,
                   "artifact": secured_artifact, "degraded": True}
        else:
            yield {"type": "error", "message": str(e)}
    finally:
        await client.close()


def _async_gen_to_sync(async_gen_factory: Callable):
    """桥接 async 生成器到 sync 生成器（线程 + queue）。

    async_gen_factory：无参可调用，返回 async 生成器
    返回：sync 生成器，产出 async 生成器的 yield 值

    线程内跑独立 event loop 消费 async 生成器，把事件 push 到 queue；
    sync 端从 queue pull 逐项 yield。错误以 {"_error": ...} 包传递。
    """
    import queue
    import threading

    _SENTINEL = object()
    q: "queue.Queue" = queue.Queue()

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _consume():
            async for item in async_gen_factory():
                q.put(item)

        try:
            loop.run_until_complete(_consume())
        except Exception as e:
            q.put({"_error": str(e)})
        finally:
            loop.close()
            q.put(_SENTINEL)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()

    while True:
        item = q.get()
        if item is _SENTINEL:
            break
        if isinstance(item, dict) and "_error" in item:
            yield {"type": "error", "message": item["_error"]}
            break
        yield item


def _make_react_invoke(
    *, user_prompt: str, dep: dict, ctx: AccessContext,
    react_runner: Callable | None = None,
) -> Callable:
    """构建 ReAct 模式的 fake_invoke 闭包（走 call_llm 闸门注入点）。

    react_runner：测试注入用（替代 _run_react_agent_async，避免依赖
    AgentScope / MCP 子进程）；生产为 None → 默认走 _run_react_agent_async。
    model 名称由 call_llm 闸门传入 _invoke 的 model 参数（已过白名单）。
    """
    def _invoke(model, prompt, redacted_input):
        try:
            if react_runner is not None:
                # 测试注入：runner 直接返回最终文本（无 artifact）
                coro = react_runner(user_prompt=user_prompt, dep=dep,
                                    model_name=model, ctx=ctx)
                text = asyncio.run(coro) if asyncio.iscoroutine(coro) else coro
                return {"content": text}
            # 生产：sync 上下文跑 async agent（chat 端点在线程池执行）
            collected = asyncio.run(_run_react_agent_async(
                user_prompt=user_prompt, dep=dep,
                model_name=model, ctx=ctx))
            return {
                "content": collected.get("text", ""),
                "artifact": collected.get("artifact"),
            }
        except ImportError as e:
            raise RuntimeError(
                f"AgentScope 未安装，react 模式不可用：{e}")
    return _invoke


def chat_on_canvas(
    *,
    conn,
    ctx: AccessContext,
    doc: dict[str, Any],
    question: str,
    pack_id: str = "default",
    base_dir=None,
    llm_client: LLMClient | None = None,
    chat_mode: str | None = None,
    case_id: str = "",
    clue_id: str = "",
    react_runner: Callable | None = None,
) -> dict[str, Any]:
    """画布只读问答主流程。

    参数：
      chat_mode：None=策略默认 / "direct"=单轮 LLM / "react"=ReAct 智能助手
      case_id/clue_id：ReAct 模式下供 agent 调工具参照（direct 模式不使用）
      react_runner：测试注入用（替代 _run_react_agent_async）

    返回：
      {ok, answer, facts, pending, warnings, citations, model, error?}
    ok=False 时含 error（LLM 被拒/失败的业务原因）。
    """
    policy = load_llm_policy(pack_id, base_dir=base_dir)

    # llm_enabled 总开关（REQ-040）
    if not policy.get("llm_enabled", False):
        return {
            "ok": False,
            "answer": "",
            "facts": [],
            "pending": [],
            "warnings": ["当前案件未启用智能问答（llm_enabled=false）"],
            "citations": [],
            "model": None,
            "error": "llm_disabled",
        }

    # 部署档选择（local/web 交集，fail-closed）
    mode, dep, off_reason = select_deployment(ctx, policy)
    if mode == "off" or dep is None:
        return {
            "ok": False,
            "answer": "",
            "facts": [],
            "pending": [],
            "warnings": [f"智能问答不可用：{off_reason}"],
            "citations": [],
            "model": None,
            "error": "deployment_off",
        }

    eff_policy = _deployment_policy(policy, dep)
    model = (dep.get("allowed_models") or [""])[0] or policy.get(
        "allowed_models", [""])[0]
    if not model:
        return {
            "ok": False,
            "answer": "", "facts": [], "pending": [],
            "warnings": ["无可用模型"],
            "citations": [], "model": None, "error": "no_model",
        }

    # 双模式开关：策略默认 → 请求覆盖 → 兜底 direct
    policy_mode = policy.get("canvas_chat_mode", "direct")
    eff_mode = chat_mode or policy_mode
    if eff_mode not in ("direct", "react"):
        eff_mode = "direct"

    # 构建上下文 + 脱敏
    context = build_canvas_context(doc)
    redacted = redact_payload(context, eff_policy)
    blob = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    redacted["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    client = llm_client or LLMClient()

    if eff_mode == "react":
        # ReAct 模式：user prompt 含 case/clue 上下文供工具参照
        user_prompt = _make_react_user_prompt(
            question, context, case_id, clue_id)
        _invoke = _make_react_invoke(
            user_prompt=user_prompt, dep=dep,
            ctx=ctx, react_runner=react_runner)
    else:
        # direct 模式：单轮 LLM 调用
        user_prompt = _make_user_prompt(question, context)
        def _invoke(model, prompt, redacted_input):  # pragma: no cover - 生产路径
            resp = client.chat(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=2048)
            if resp.get("ok"):
                return resp["result"]
            raise RuntimeError(resp.get("error", "LLM 调用失败"))

    try:
        llm_result = call_llm(
            conn, ctx, eff_policy, model=model, prompt=user_prompt,
            redacted_input=redacted, fake_invoke=_invoke)
    except LLMBlockedError as e:
        return {
            "ok": False, "answer": "", "facts": [], "pending": [],
            "warnings": [f"智能问答被治理策略拒绝：{e}"],
            "citations": [], "model": model, "error": "llm_blocked",
        }
    except Exception as e:
        err_msg = str(e)
        # ReAct 模式 AgentScope 缺失 → 业务错误 react_unavailable
        if "AgentScope 未安装" in err_msg:
            return {
                "ok": False, "answer": "", "facts": [], "pending": [],
                "warnings": [f"ReAct 模式不可用：{err_msg}"],
                "citations": [], "model": model,
                "error": "react_unavailable",
            }
        # ReAct 偶发 400：DashScope qwen-plus 生成不合法 function.arguments
        # 已重试仍失败 → 引导用户重试或切回 direct 模式
        if "function.arguments" in err_msg and "must be in JSON format" in err_msg:
            return {
                "ok": False, "answer": "", "facts": [], "pending": [],
                "warnings": [
                    "ReAct 模式调用工具时模型偶发生成参数失败，"
                    "请重试，或切换为「单轮」模式。"
                ],
                "citations": [], "model": model,
                "error": "react_tool_arg_invalid",
            }
        return {
            "ok": False, "answer": "", "facts": [], "pending": [],
            "warnings": [f"智能问答失败：{e}"],
            "citations": [], "model": model, "error": "llm_error",
        }

    raw = llm_result.get("result") or {}
    if isinstance(raw, dict):
        raw_content = raw.get("content", "")
        artifact = raw.get("artifact")
    else:
        raw_content = str(raw)
        artifact = None

    # 工具确定性产物直通（如 report.render 的 md 报告）：正文取产物原文，
    # 不走逐句 [cite:] 分流——产物完整性由确定性管道保证，报告内部
    # 已含「待核实事项」段与降级声明，LLM 文本不作为答复正文。
    if isinstance(artifact, dict) and artifact.get("text"):
        return {
            "ok": True,
            "answer": artifact["text"],
            "facts": [],
            "pending": [],
            "warnings": [],
            "citations": [],
            "model": model,
            "artifact": artifact,
        }

    # RC-302 引用校验
    valid_refs = build_valid_refs_from_doc(doc)
    guarded = validate_citations(raw_content, valid_refs)

    return {
        "ok": True,
        "answer": raw_content,
        "facts": guarded["facts"],
        "pending": guarded["pending"],
        "warnings": guarded["warnings"],
        "citations": [c for f in guarded["facts"] for c in f["citations"]],
        "model": model,
        "artifact": None,
    }


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:16]


# ----------------------------------------------------------------------
# 流式问答（SSE 事件生成器）
# ----------------------------------------------------------------------

def _chat_event(event: str, data: dict) -> dict:
    """结构化 SSE 事件（路由层格式化为 text/event-stream 帧）。"""
    return {"event": event, "data": data}


def chat_on_canvas_stream(
    *,
    conn,
    ctx: AccessContext,
    doc: dict[str, Any],
    question: str,
    pack_id: str = "default",
    base_dir=None,
    llm_client: LLMClient | None = None,
    chat_mode: str | None = None,
    case_id: str = "",
    clue_id: str = "",
    react_runner: Callable | None = None,
):
    """画布只读问答流式主流程（生成器，产出结构化 SSE 事件 dict）。

    事件类型：
      start  — {model} 流式开始
      delta  — {text} 增量文本块
      done   — {answer, facts, pending, warnings, citations, model} 流式结束
      error  — {error, message} 业务错误（非 500）

    direct 模式走 LLMClient.chat_stream 逐 token 流式；
    react 模式先非流式完成，再一次性发 done（不逐 token）。
    红线同 chat_on_canvas：只读、脱敏、引用校验。
    """
    policy = load_llm_policy(pack_id, base_dir=base_dir)

    if not policy.get("llm_enabled", False):
        yield _chat_event("error", {
            "error": "llm_disabled",
            "message": "当前案件未启用智能问答（llm_enabled=false）",
        })
        return

    mode, dep, off_reason = select_deployment(ctx, policy)
    if mode == "off" or dep is None:
        yield _chat_event("error", {
            "error": "deployment_off",
            "message": f"智能问答不可用：{off_reason}",
        })
        return

    eff_policy = _deployment_policy(policy, dep)
    model = (dep.get("allowed_models") or [""])[0] or policy.get(
        "allowed_models", [""])[0]
    if not model:
        yield _chat_event("error", {
            "error": "no_model",
            "message": "无可用模型",
        })
        return

    policy_mode = policy.get("canvas_chat_mode", "direct")
    eff_mode = chat_mode or policy_mode
    if eff_mode not in ("direct", "react"):
        eff_mode = "direct"

    # 构建上下文 + 脱敏
    context = build_canvas_context(doc)
    redacted = redact_payload(context, eff_policy)
    blob = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    redacted["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    client = llm_client or LLMClient()

    # ---- react 模式：流式事件桥接（思考/工具/答案逐事件推送）----
    if eff_mode == "react":
        user_prompt = _make_react_user_prompt(
            question, context, case_id, clue_id)

        # ReAct 走 call_llm_stream 闸门（复用网络/白名单/脱敏/日志检查），
        # 通过后启动 async 事件桥接器。
        # react_runner 注入点：测试用（绕过 AgentScope，直接返回文本）
        def _react_streaming_invoke(model, prompt, redacted_input):
            if react_runner is not None:
                # 测试注入：react_runner 返回最终文本，不逐 token
                try:
                    coro = react_runner(
                        user_prompt=user_prompt, dep=dep,
                        model_name=model, ctx=ctx)
                    if asyncio.iscoroutine(coro):
                        text = asyncio.run(coro)
                    else:
                        text = coro
                    yield {"type": "done", "text": text}
                except Exception as e:
                    yield {"type": "error", "message": str(e)}
            else:
                async_gen_factory = lambda: _run_react_agent_stream(
                    user_prompt=user_prompt, dep=dep, model_name=model,
                    ctx=ctx)
                yield from _async_gen_to_sync(async_gen_factory)

        try:
            gen = call_llm_stream(
                conn, ctx, eff_policy, model=model, prompt=user_prompt,
                redacted_input=redacted,
                streaming_invoke=_react_streaming_invoke)
        except LLMBlockedError as e:
            yield _chat_event("error", {
                "error": "llm_blocked",
                "message": f"智能问答被治理策略拒绝：{e}",
            })
            return
        except Exception as e:
            err_msg = str(e)
            # ReAct 偶发 400：DashScope 生成不合法 function.arguments
            if "function.arguments" in err_msg and \
                    "must be in JSON format" in err_msg:
                yield _chat_event("error", {
                    "error": "react_tool_arg_invalid",
                    "message": "ReAct 模式调用工具时模型偶发生成参数失败，"
                              "请重试，或切换为「单轮」模式。",
                })
            else:
                yield _chat_event("error", {
                    "error": "llm_error",
                    "message": f"智能问答失败：{e}",
                })
            return

        yield _chat_event("start", {"model": model})

        final_answer = ""
        final_artifact = None
        for evt in gen:
            evt_type = evt.get("type")
            if evt_type == "thinking":
                yield _chat_event("thinking", {"text": evt["text"]})
            elif evt_type == "delta":
                final_answer += evt["text"]
                yield _chat_event("delta", {"text": evt["text"]})
            elif evt_type == "tool_call":
                yield _chat_event("tool_call", {
                    "name": evt["name"], "id": evt["id"]})
            elif evt_type == "tool_result":
                yield _chat_event("tool_result", {
                    "name": evt["name"], "id": evt["id"],
                    "state": evt.get("state", "")})
            elif evt_type == "done":
                # 桥接器产出 done 时拿最终文本（覆盖累积，防漏）
                if evt.get("text"):
                    final_answer = evt["text"]
                final_artifact = evt.get("artifact")
                break
            elif evt_type == "error":
                err_msg = evt["message"]
                if "AgentScope 未安装" in err_msg:
                    yield _chat_event("error", {
                        "error": "react_unavailable",
                        "message": f"ReAct 模式不可用：{err_msg}",
                    })
                elif "function.arguments" in err_msg and \
                        "must be in JSON format" in err_msg:
                    yield _chat_event("error", {
                        "error": "react_tool_arg_invalid",
                        "message": "ReAct 模式调用工具时模型偶发生成参数失败，"
                                  "请重试，或切换为「单轮」模式。",
                    })
                else:
                    yield _chat_event("error", {
                        "error": "llm_error",
                        "message": f"智能问答失败：{err_msg}",
                    })
                return

        # 工具确定性产物直通（如 report.render 的 md 报告）：答复正文取
        # 产物原文，绕过逐句 [cite:] 分流（理由同 chat_on_canvas）。
        # 模型流式期间产出的一句话总结被产物原文覆盖，不展示给分析师。
        if isinstance(final_artifact, dict) and final_artifact.get("text"):
            yield _chat_event("done", {
                "answer": final_artifact["text"],
                "facts": [],
                "pending": [],
                "warnings": [],
                "citations": [],
                "model": model,
                "artifact": final_artifact,
            })
            return

        # RC-302 引用校验
        valid_refs = build_valid_refs_from_doc(doc)
        guarded = validate_citations(final_answer, valid_refs)

        yield _chat_event("done", {
            "answer": final_answer,
            "facts": guarded["facts"],
            "pending": guarded["pending"],
            "warnings": guarded["warnings"],
            "citations": [c for f in guarded["facts"]
                          for c in f["citations"]],
            "model": model,
            "artifact": None,
        })
        return

    # ---- direct 模式：逐 token 流式 ----
    user_prompt = _make_user_prompt(question, context)

    def _streaming_invoke(model, prompt, redacted_input):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        yield from client.chat_stream(messages, temperature=0.3,
                                      max_tokens=2048)

    try:
        gen = call_llm_stream(
            conn, ctx, eff_policy, model=model, prompt=user_prompt,
            redacted_input=redacted, streaming_invoke=_streaming_invoke)
    except LLMBlockedError as e:
        yield _chat_event("error", {
            "error": "llm_blocked",
            "message": f"智能问答被治理策略拒绝：{e}",
        })
        return
    except Exception as e:
        yield _chat_event("error", {
            "error": "llm_error",
            "message": f"智能问答失败：{e}",
        })
        return

    yield _chat_event("start", {"model": model})

    full_text = ""
    for chunk in gen:
        if "delta" in chunk:
            full_text += chunk["delta"]
            yield _chat_event("delta", {"text": chunk["delta"]})
        elif "error" in chunk:
            yield _chat_event("error", {
                "error": "llm_error",
                "message": chunk["error"],
            })
            return

    # RC-302 引用校验
    valid_refs = build_valid_refs_from_doc(doc)
    guarded = validate_citations(full_text, valid_refs)

    yield _chat_event("done", {
        "answer": full_text,
        "facts": guarded["facts"],
        "pending": guarded["pending"],
        "warnings": guarded["warnings"],
        "citations": [c for f in guarded["facts"] for c in f["citations"]],
        "model": model,
        "artifact": None,
    })
