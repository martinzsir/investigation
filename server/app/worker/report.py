"""
server/app/worker/report.py
RC-304 TASK_REPORT：研判报告异步生成处理器。

与 worker/verify.py 同纪律：
  - 业务面短频写：只写 per-case state.sqlite（clue_canvas_report + audit_chain）；
  - operator/role/clearance 为入队时会话快照，Worker 不信任请求体外的身份；
  - 读快照 doc → gather_evidence 采集确定性块 → 构建 prompt → call_llm
    → parse_llm_to_sections → render_report 渲染 → 写回 report 行；
  - 采集/渲染失败降级，不中断主流程；
  - 失败 → status=failed + error 记录，不崩 Worker（转 TaskExecError）。

错误码：
  CASE_NOT_FOUND / NO_VERSION / REPORT_NOT_FOUND / SNAPSHOT_NOT_FOUND /
  LLM_DISABLED / LLM_BLOCKED / LLM_ERROR / REPORT_PARSE_FAILED
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from typing import Any

from core.access import AccessContext, LLMBlockedError
from core.llm.draft_verify import select_deployment, _deployment_policy
from core.llm.llm_client import LLMClient
from core.llm.redact import (
    call_llm,
    load_llm_policy,
    redact_payload,
)

from server.app.canvas_citation_guard import build_valid_refs_from_doc
from server.app.canvas_report import (
    build_report_prompt,
    parse_llm_to_sections,
    parse_report_sections,
)
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# sunzi-report skill 桥接（惰性加载 .trae/skills/sunzi-report/scripts/）
# ----------------------------------------------------------------------

def _project_root() -> Path:
    """定位项目根（server/app/worker/ 向上三层）。"""
    return Path(__file__).resolve().parents[3]


def _load_skill_module(script_name: str):
    """惰性加载 sunzi-report skill 脚本为模块。"""
    path = (_project_root() / ".trae" / "skills" / "sunzi-report"
            / "scripts" / f"{script_name}.py")
    if not path.exists():
        raise FileNotFoundError(
            f"sunzi-report skill 脚本不存在：{path}")
    spec = importlib.util.spec_from_file_location(
        f"_sunzi_skill_{script_name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 skill 脚本：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gather_evidence(case_id: str, clue_id: str,
                     pack: str = "default") -> dict[str, Any]:
    """调用 sunzi-report skill 的 gather_evidence 采集确定性证据。

    红规：
      - 第二/五段数字由脚本确定性产出，LLM 不得改写；
      - 降级项非空时报告必须如实标注；
      - 输出已脱敏。
    """
    ge = _load_skill_module("gather_evidence")
    root = ge.find_project_root(str(_project_root()))
    degraded: list[str] = []

    # 案件数据版本
    version: int | None = None
    case_dir = root / "cases" / case_id
    if case_dir.exists():
        vs = sorted(
            (int(p.stem[1:]) for p in case_dir.glob("v*.duckdb")
             if p.stem[1:].isdigit()),
            reverse=True,
        )
        version = vs[0] if vs else None
    else:
        degraded.append(f"案件库不存在：{case_dir}（未 BUILD 或 case_id 有误）")

    if version is None:
        degraded.append("未解析到 data_version：报告将缺少数据版本锚点")

    # P8：人验通过的图像证据（state.sqlite；无库=空，查询失败记降级）——
    # 与 MCP report.gather_evidence / CLI main() 同构；漏接会让八段书证
    # 退化为 LLM 转述，发现级内容（title/severity/人验结论）不进报告
    image_evidence = ge.collect_image_evidence_safe(
        root, case_id, clue_id, degraded)

    evidence = {
        "case_id": case_id,
        "clue_id": clue_id,
        "pack": pack,
        "data_version": version,
        "确定性块": {
            "证据充分性": ge.collect_cross(root, pack, degraded),
            "关联核验": {
                "过桥路径": ge.collect_overpass(root, degraded),
                "规则手册": ge.load_rules(root, pack),
            },
            # 第九段：ingest 登记 + 本线索画布引用（与 MCP gather 同构；
            # 漏接会让第九段落「未知（采集端未提供登记）」占位）
            "数据源清单": ge.collect_sources(
                root, case_id, clue_id, pack, version, degraded),
            # P7：线索 evidence_refs 自动聚合（图谱/资金/书证引用）
            "线索证据引用": ge.collect_clue_refs(
                root, case_id, clue_id, degraded),
            # P8：人验通过的图像证据（发现级内容确定性入报告）
            "图像证据": image_evidence,
        },
        "降级": degraded,
        "脱敏": True,
        "生成方式": "deterministic（无 LLM 参与）",
    }
    return ge.redact(evidence)


def _render_report_md(evidence: dict, sections: dict,
                      rtype: str = "A") -> str:
    """调用 sunzi-report skill 的 render_report 渲染最终 Markdown。"""
    rr = _load_skill_module("render_report")
    return rr.build_markdown(evidence, sections, rtype)


def handle_report(task, *, repo, factory, snapshot_base_for=None, **_: Any) \
        -> dict[str, Any]:
    """研判报告生成（RC-304）。

    task.params: {report_id, clue_id, operator, role, clearance, extra_request?}
    """
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")

    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无报告可生成")

    p = task.params or {}
    report_id = str(p.get("report_id") or "").strip()
    clue_id = str(p.get("clue_id") or "").strip()
    operator = str(p.get("operator") or "").strip()
    role = str(p.get("role") or "正兵")
    clearance = int(p.get("clearance") or 1)
    extra_request = str(p.get("extra_request") or "").strip()

    if not report_id:
        raise TaskExecError("REPORT_NOT_FOUND", "缺少 report_id")
    if not clue_id:
        raise TaskExecError("CLUE_REQUIRED", "缺少 clue_id")

    state_path = factory.case_dir(case.id) / "state.sqlite"
    state = StateStore(case.id, state_path)
    try:
        return _do_generate(
            state, repo, case, ver, report_id, clue_id,
            operator, role, clearance, extra_request,
            snapshot_base_for, task.id)
    finally:
        state.close()


def _do_generate(state, repo, case, ver, report_id, clue_id,
                 operator, role, clearance, extra_request,
                 snapshot_base_for, task_id: str) -> dict[str, Any]:
    """报告生成主流程。"""
    # 1. 取 report 行（确认存在且 status=generating）
    report = state.get_canvas_report(report_id)
    if report is None:
        raise TaskExecError(
            "REPORT_NOT_FOUND", f"报告不存在：{report_id}")
    snapshot_id = report.get("snapshot_id", "")
    if not snapshot_id:
        raise TaskExecError(
            "SNAPSHOT_NOT_FOUND", f"报告未关联快照：{report_id}")

    # 2. 取冻结快照 doc
    snap = state.get_canvas_snapshot(snapshot_id)
    if snap is None:
        raise TaskExecError(
            "SNAPSHOT_NOT_FOUND", f"快照不存在：{snapshot_id}")
    doc = snap["doc"]

    # 3. 构建 AccessContext + LLM 策略
    base_dir = None
    if snapshot_base_for:
        base_dir = snapshot_base_for(case.id)

    access = AccessContext(
        operator=operator, role=role, case_id=case.id,
        purpose="研判报告生成", clearance=clearance)

    policy = load_llm_policy(case.pack_id, base_dir=base_dir)
    if not policy.get("llm_enabled", False):
        state.update_canvas_report_status(
            report_id, status="failed",
            error="llm_disabled")
        raise TaskExecError(
            "LLM_DISABLED", "当前案件未启用 LLM（llm_enabled=false）")

    mode, dep, off_reason = select_deployment(access, policy)
    if mode == "off" or dep is None:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"deployment_off: {off_reason}")
        raise TaskExecError(
            "LLM_BLOCKED", f"智能问答不可用：{off_reason}")

    eff_policy = _deployment_policy(policy, dep)
    model = (dep.get("allowed_models") or [""])[0] or policy.get(
        "allowed_models", [""])[0]
    if not model:
        state.update_canvas_report_status(
            report_id, status="failed", error="no_model")
        raise TaskExecError("LLM_BLOCKED", "无可用模型")

    # 4. 调用 sunzi-report skill 采集确定性证据
    evidence: dict[str, Any] | None = None
    try:
        evidence = _gather_evidence(case.id, clue_id, case.pack_id)
    except Exception as e:
        # 采集失败不中断，降级为无确定性证据
        evidence = {
            "case_id": case.id, "clue_id": clue_id,
            "pack": case.pack_id, "data_version": ver,
            "确定性块": {},
            "降级": [f"确定性证据采集失败：{e}"],
            "脱敏": True,
            "生成方式": "deterministic（降级）",
        }

    # 5. 构建 prompt + 脱敏
    system_prompt, user_prompt = build_report_prompt(
        doc, extra_request, evidence)
    import hashlib
    import json
    from server.app.canvas_chat import build_canvas_context
    context = build_canvas_context(doc)
    redacted = redact_payload(context, eff_policy)
    blob = json.dumps(redacted, ensure_ascii=False,
                      sort_keys=True, default=str)
    redacted["redaction_hash"] = hashlib.sha256(
        blob.encode("utf-8")).hexdigest()

    # 6. call_llm
    client = LLMClient()

    def _invoke(model, prompt, redacted_input):  # pragma: no cover
        resp = client.chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=4096)
        if resp.get("ok"):
            return resp["result"]
        raise RuntimeError(resp.get("error", "LLM 调用失败"))

    try:
        llm_result = call_llm(
            state.conn, access, eff_policy, model=model,
            prompt=user_prompt, redacted_input=redacted,
            fake_invoke=_invoke)
    except LLMBlockedError as e:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"llm_blocked: {e}")
        raise TaskExecError("LLM_BLOCKED", f"LLM 被治理策略拒绝：{e}")
    except Exception as e:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"llm_error: {e}")
        raise TaskExecError("LLM_ERROR", f"LLM 调用失败：{e}")

    # 7. 解析 LLM 输出 + 引用校验
    raw = llm_result.get("result") or {}
    raw_content = raw.get("content", "") if isinstance(raw, dict) \
        else str(raw)
    valid_refs = build_valid_refs_from_doc(doc)

    try:
        parsed = parse_llm_to_sections(raw_content, valid_refs)
    except Exception as e:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"parse_failed: {e}")
        raise TaskExecError(
            "REPORT_PARSE_FAILED", f"报告解析失败：{e}")

    sections = parsed["sections"]
    citations = parsed["citations"]
    warnings = parsed["warnings"]

    # 8. 调用 sunzi-report skill 渲染最终 Markdown
    #    将 citations 传入 sections 供 render_report 生成附录
    sections["citations"] = citations
    try:
        content_md = _render_report_md(evidence, sections, "A")
    except Exception as e:
        # 渲染失败降级：用 parse_report_sections 组装 content_md
        fallback_parsed = parse_report_sections(raw_content, valid_refs)
        content_md = fallback_parsed["content_md"]
        warnings = (warnings or []) + [
            f"render_report 降级：{e}"]

    # 9. 写回 report 行
    state.update_canvas_report_status(
        report_id, status="ready",
        content_md=content_md,
        sections=sections,
        citations=citations,
        warnings=warnings,
        task_id=task_id)

    return {
        "report_id": report_id,
        "status": "ready",
        "version_no": report.get("version_no", 0),
        "sections": list(sections.keys()),
        "citation_count": len(citations),
        "warning_count": len(warnings),
    }
