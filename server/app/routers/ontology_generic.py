"""
server/app/routers/ontology_generic.py
S5 通用本体文件读写 + 影响面分析 + 轻量提案（通用 schema 驱动表单的后端）。

读：
  GET /cases/{cid}/ontology/files/{name}
      原始 JSON 文档 + 对应 JSON Schema + 可写标记。
      🔴 llm_policy 仅只读展示：writable=false / form_enabled=false（R6，永不生成表单）。

影响面（F3，纯只读，登录可访问）：
  POST /cases/{cid}/ontology/impact  {file, doc}
      服务端以磁盘当前文档为 old、请求文档为 new，实时计算五类影响
      （已生成线索/规则/视图/已物化表/函数）+ 动态 SQL 不确定引用。
      影响面失败绝不返回「无影响」：status=failed/partial + failures 显式列出。

提案轻量版（F4，状态机 draft→impact_ready→published/discarded）：
  POST   /cases/{cid}/ontology/proposals                 提出（草稿，不生效）
  POST   /cases/{cid}/ontology/proposals/{pid}/impact    服务端计算并挂载影响面
  POST   /cases/{cid}/ontology/proposals/{pid}/publish   🔴 人工发布（唯一生效入口）
  POST   /cases/{cid}/ontology/proposals/{pid}/discard   废弃（本体不变）
  GET    /cases/{cid}/ontology/proposals/{pid}

🔴 R4：无任何自动发布路径（worker/LLM 不读提案库）；E4-1 未看影响面发布被拒；
E4-3 影响面基准变化发布被拒（须重新评估）。发布走 save_config_json 临时副本
load_pack 全量校验 + record_config_audit（版本沿革含操作人/理由），不自动重跑
BUILD、不迁移已生成线索（R3）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.impact import (
    ANALYZABLE_FILES,
    ImpactInput,
    compute_impact,
    impact_fingerprint,
)
from core.ontology_loader import load_pack

from server.app.clues_artifact import latest_artifact_version
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, ERR_NOT_FOUND, ERR_VALIDATION, APIError, ok
from server.app.ontology_proposals import OntologyProposalStore, ProposalError
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.snapshot_config import (
    record_config_audit,
    require_analyst,
    save_config_json,
    snapshot_paths,
    commit_ontology_write,
)
from server.app.store.state_store import StateStore

router = APIRouter(tags=["ontology-generic"])

# 通用表单可写文件（S2-S4 专用编辑器所有的 objects/links/rules/policies/actions/
# states/data_elements 不走通用表单，R5；case_knowledge 由知识包页承载）。
GENERIC_FILES = frozenset({
    "derived_properties", "dimensions", "enum_space", "jians",
    "scoring", "thresholds", "verify_playbooks",
})
# 原始 JSON 可读文件（llm_policy 只读展示，UC-S5-7）
READABLE_FILES = GENERIC_FILES | {"llm_policy"}
# 永不开放：无读模型、无表单（R6）
NEVER_FORM = frozenset({"llm_policy"})


def _schema_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "schemas"


def _load_schema(name: str) -> dict | None:
    p = _schema_dir() / f"{name}.schema.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _read_doc(snap_dir: Path, name: str) -> dict:
    path = snap_dir / f"{name}.json"
    if not path.is_file():
        raise APIError(ERR_NOT_FOUND, f"本体文件不存在：{name}.json", 404)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise APIError(ERR_VALIDATION, f"{name}.json 解析失败：{e}", 400)


def _proposal_store(ctx: WebContext, case_id: str) -> OntologyProposalStore:
    return OntologyProposalStore(
        ctx.factory.case_dir(case_id) / "ontology_proposals.sqlite")


# ----------------------------------------------------------------------
# 原始文档 + schema 下发
# ----------------------------------------------------------------------
@router.get("/cases/{case_id}/ontology/files/{name}")
def get_ontology_file(case_id: str, name: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """通用表单数据源：原始文档 + schema + 可写/可表单标记。"""
    _get_owned_case(case_id, p, ctx.cases)
    if name not in READABLE_FILES:
        raise APIError(ERR_NOT_FOUND,
                       f"通用编辑器不承载该文件：{name}（请使用专用编辑器）", 404)
    _pack_id, snap_dir, _base = snapshot_paths(ctx, case_id)
    doc = _read_doc(snap_dir, name)
    return ok({
        "file": name,
        "writable": name in GENERIC_FILES,
        "form_enabled": name not in NEVER_FORM,
        "schema": _load_schema(name),
        "doc": doc,
    }, data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# 影响面装配
# ----------------------------------------------------------------------
def _materialized_tables(ctx: WebContext, case_id: str):
    """(obj_*/lnk_* 表名清单, error)。从未 BUILD（版本文件不存在）= 真空表清单。"""
    try:
        version = ctx.repo.current_version(case_id)
    except Exception:  # noqa: BLE001
        return [], None
    path = ctx.factory.version_path(case_id, version)
    if not path.is_file():
        return [], None
    try:
        store = ctx.factory.for_case(case_id, mode="read", version=version)
        try:
            rows = store.read_conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name LIKE 'obj_%' OR table_name LIKE 'lnk_%'"
            ).fetchall()
            return [r[0] for r in rows], None
        finally:
            store.close()
    except Exception as e:  # noqa: BLE001 —— D10：不可读 ≠ 无表
        return None, f"DuckDB 不可读：{type(e).__name__}: {e}"[:180]


def _latest_clues(ctx: WebContext, case_id: str):
    """(线索列表|None, 产物 Path|None)。无产物=[]（真空）；损坏=None（unavailable）。"""
    case_dir = ctx.factory.case_dir(case_id)
    ver = latest_artifact_version(case_dir)
    if ver is None:
        return [], None
    path = case_dir / "artifacts" / f"clues_v{ver}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("clues", []), path
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None, path


def _clue_status_map(ctx: WebContext, case_id: str) -> dict[str, str]:
    db = ctx.factory.case_dir(case_id) / "state.sqlite"
    if not db.is_file():
        return {}
    try:
        st = StateStore(case_id, str(db))
        return {cid: v["status"] for cid, v in st.status_map().items()}
    except Exception:  # noqa: BLE001 —— 状态缺失不影响影响面主流程
        return {}


def _standard_layer(ctx: WebContext, case_id: str, file: str,
                    pack_id: str) -> dict:
    """§14-2：同名文件存在于全域/行业层时，提示影响 N 个案件。"""
    base = ctx.cases.snapshot_ontology_root(case_id)
    shared_p = base / "_shared" / f"{file}.json"
    industry = None
    meta_p = ctx.cases.snapshot_dir(case_id, pack_id) / "pack_meta.json"
    if meta_p.is_file():
        try:
            industry = json.loads(meta_p.read_text(
                encoding="utf-8")).get("industry")
        except (json.JSONDecodeError, OSError):
            industry = None
    industry_p = bool(industry) and (
        base / "_industry" / industry / f"{file}.json").is_file()
    layer = "shared" if shared_p.is_file() else ("industry" if industry_p else None)
    affected = 0
    if layer:
        root = ctx.factory.cases_root
        affected = sum(
            1 for cd in root.iterdir()
            if cd.is_dir() and any((cd / "ontology").glob("*/objects.json"))) \
            if root.is_dir() else 0
    return {"layer": layer, "affected_cases": affected,
            "industry": industry}


def _build_impact(ctx: WebContext, case_id: str, file: str,
                 new_doc: dict) -> tuple[dict, str]:
    """装配入参并计算影响面；返回 (impact, fingerprint)。调用方先做案件鉴权。"""
    pack_id, snap_dir, _base = snapshot_paths(ctx, case_id)
    old_doc = _read_doc(snap_dir, file)
    tables, tables_err = _materialized_tables(ctx, case_id)
    clues, artifact_path = _latest_clues(ctx, case_id)
    status_map = _clue_status_map(ctx, case_id)
    standard = _standard_layer(ctx, case_id, file, pack_id)

    inp = ImpactInput(
        file=file, old_doc=old_doc, new_doc=new_doc, pack_dir=snap_dir,
        materialized_tables=None if tables_err else tables,
        clues=clues, clue_status=status_map, standard_layer=standard,
    )
    # 注：tables_err 时传 materialized_tables=None，引擎已把 tables 类置
    # unavailable 并写 failures（D10），这里不再重复记录。
    _ = tables_err
    impact = compute_impact(inp)
    impact["standard_layer"] = standard
    fp = impact_fingerprint(
        snap_dir, materialized_tables=tables, clues_artifact=artifact_path)
    return impact, fp


class ImpactIn(BaseModel):
    file: str
    doc: dict


@router.post("/cases/{case_id}/ontology/impact")
def impact_preview(case_id: str, body: ImpactIn,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """影响面预览（不落任何状态）。查看权限=案件可访问。"""
    _get_owned_case(case_id, p, ctx.cases)
    if body.file not in ANALYZABLE_FILES:
        raise APIError(ERR_VALIDATION,
                       f"不支持影响面分析的文件：{body.file}（llm_policy 永不开放）",
                       400)
    impact, fp = _build_impact(ctx, case_id, body.file, body.doc)
    return ok({"impact": impact, "fingerprint": fp})


# ----------------------------------------------------------------------
# 提案
# ----------------------------------------------------------------------
class ProposalIn(BaseModel):
    file: str
    doc: dict
    reason: str = ""


@router.post("/cases/{case_id}/ontology/proposals")
def create_proposal(case_id: str, body: ProposalIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """① 提出：变更入草稿（不生效、不触流程，R7）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    if body.file not in GENERIC_FILES:
        raise APIError(
            ERR_FORBIDDEN,
            f"{body.file} 不走通用提案（专用编辑器文件请走各自保存路径；"
            f"llm_policy 永不开放）", 403)
    if not isinstance(body.doc, dict):
        raise APIError(ERR_VALIDATION, "doc 必须是 JSON 对象", 400)
    if not body.reason.strip():
        raise APIError(ERR_VALIDATION,
                       "提出本体变更提案必须填写变更理由（版本沿革留痕）", 400)
    # 文档形状先过 loader 一次（草稿阶段即拦死非法文档，避免发布时才发现）
    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)
    data = dict(_read_doc(snap_dir, body.file))
    data.update(body.doc)
    try:
        save_validate_only(snap_dir, pack_id, base_dir,
                           f"{body.file}.json", data)
    except Exception as e:  # noqa: BLE001
        raise APIError(ERR_VALIDATION,
                       f"文档校验失败，提案未创建：{e}", 400)
    store = _proposal_store(ctx, case_id)
    rec = store.create(file=body.file, doc=body.doc,
                       reason=body.reason.strip(), author=p.operator)
    ctx.repo.record_ops("ontology_proposal_create", case_id,
                        {"proposal_id": rec["proposal_id"],
                         "file": body.file, "by": p.operator})
    return ok(_public_proposal(rec))


def save_validate_only(snap_dir, pack_id, base_dir, filename, data):
    """临时副本 load_pack 全量校验但不落盘（草稿创建用）。"""
    from server.app.snapshot_config import copy_layer_dirs
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        copy_layer_dirs(snap_dir, base_dir, tmp_root)
        (tmp_root / pack_id / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        load_pack(pack_id, base_dir=tmp_root)


@router.get("/cases/{case_id}/ontology/proposals/{pid}")
def get_proposal(case_id: str, pid: str,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    rec = _proposal_store(ctx, case_id).get(pid)
    if rec is None:
        raise APIError(ERR_NOT_FOUND, f"提案不存在：{pid}", 404)
    return ok(_public_proposal(rec))


@router.post("/cases/{case_id}/ontology/proposals/{pid}/impact")
def proposal_impact(case_id: str, pid: str,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """② 影响面：服务端实时计算并挂载（客户端不可自报影响面）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    store = _proposal_store(ctx, case_id)
    rec = store.get(pid)
    if rec is None:
        raise APIError(ERR_NOT_FOUND, f"提案不存在：{pid}", 404)
    impact, fp = _build_impact(ctx, case_id, rec["file"], rec["doc"])
    try:
        rec = store.attach_impact(pid, impact, fp)
    except ProposalError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)
    return ok(_public_proposal(rec))


@router.post("/cases/{case_id}/ontology/proposals/{pid}/publish")
def publish_proposal(case_id: str, pid: str,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """③ 发布：🔴 唯一人工生效入口。未看影响面/基准变化均拒绝（E4-1/E4-3）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    store = _proposal_store(ctx, case_id)
    rec = store.get(pid)
    if rec is None:
        raise APIError(ERR_NOT_FOUND, f"提案不存在：{pid}", 404)
    if rec["author"] != p.operator and not p.is_ontology_admin:
        raise APIError(ERR_FORBIDDEN,
                       "仅提案提出人或本体管理员可以发布该提案", 403)

    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)
    tables, _ = _materialized_tables(ctx, case_id)
    _, artifact_path = _latest_clues(ctx, case_id)
    fp = impact_fingerprint(
        snap_dir, materialized_tables=tables, clues_artifact=artifact_path)

    # 状态机校验（E4-1 未看影响面 / E4-3 基准变化 → 400）
    try:
        rec = store.publish(pid, p.operator, fp)
    except ProposalError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)

    # 落盘：保留磁盘文档中未在提案里出现的顶层键（F5 未知字段兜底的后端防线）
    filename = f"{rec['file']}.json"
    current = _read_doc(snap_dir, rec["file"])
    data = dict(current)
    data.update(rec["doc"])
    try:
        save_config_json(snap_dir, pack_id, base_dir, filename, data,
                         ctx=ctx, case_id=case_id,
                         op="ontology_proposal_publish", p=p,
                         detail={"proposal_id": pid, "file": filename,
                                 "impact_totals": (rec["impact"] or {}).get(
                                     "totals", {})})
    except APIError:
        raise
    except Exception as e:  # noqa: BLE001
        raise APIError(ERR_VALIDATION,
                       f"{filename} 校验失败，发布未落盘：{e}", 400)
    ontology_fp = commit_ontology_write(
        ctx, case_id, op="ontology_proposal_publish",
        operator=p.operator, reason=rec["reason"], changed_files=[filename])
    record_config_audit(ctx, case_id, p, "ontology_proposal_publish",
                        filename=filename, reason=rec["reason"],
                        summary={"proposal_id": pid,
                                 "impact": (rec["impact"] or {}).get("totals", {})})
    return ok({"published": True, "proposal_id": pid,
               "ontology_version": ontology_fp,
               "needs_rebuild": bool(
                   (rec["impact"] or {}).get("totals", {}).get("tables"))},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/ontology/proposals/{pid}/discard")
def discard_proposal(case_id: str, pid: str,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """废弃提案（终止态；本体文件不变，UC-S5-18）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    store = _proposal_store(ctx, case_id)
    rec = store.get(pid)
    if rec is None:
        raise APIError(ERR_NOT_FOUND, f"提案不存在：{pid}", 404)
    if rec["author"] != p.operator and not p.is_ontology_admin:
        raise APIError(ERR_FORBIDDEN,
                       "仅提案提出人或本体管理员可以废弃该提案", 403)
    try:
        rec = store.discard(pid, p.operator)
    except ProposalError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)
    return ok(_public_proposal(rec))


def _public_proposal(rec: dict) -> dict:
    return {
        "proposal_id": rec["proposal_id"],
        "file": rec["file"],
        "status": rec["status"],
        "reason": rec["reason"],
        "author": rec["author"],
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
        "published_at": rec["published_at"],
        "published_by": rec["published_by"],
        "discarded_by": rec["discarded_by"],
        "impact": rec["impact"],
    }
