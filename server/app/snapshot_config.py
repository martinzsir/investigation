"""
server/app/snapshot_config.py
M4 配置类读写共享助手（W-013/015/016/017 复用 rule_workshop 范式）。

核心：配置文件写案件快照（cases/{cid}/ontology/<pack>/xxx.json），
写盘前在临时副本上过 core.ontology_loader.load_pack 全量强校验，
不合法不落盘；合法后 os.replace 原子替换。

与 rule_workshop.py 同构，抽取共享以避免重复。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from core.ontology_loader import load_pack
from core.access import ROLE_RANK

from server.app.deps import WebContext
from server.app.envelope import ERR_FORBIDDEN, ERR_NOT_FOUND, APIError
from server.app.meta.models import PackSnapshotHistory
from server.app.security import Principal


def snapshot_paths(ctx: WebContext, case_id: str
                   ) -> tuple[str, Path, Path]:
    """返回 (pack_id, 快照包目录, 快照 base_dir)。"""
    case = ctx.repo.get_case(case_id)
    pack_id = case.pack_id if case else "default"
    snap_dir = ctx.cases.snapshot_dir(case_id, pack_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    if not snap_dir.exists():
        raise APIError(ERR_NOT_FOUND, f"案件快照不存在：{case_id}", 404)
    return pack_id, snap_dir, base_dir


def require_analyst(p: Principal) -> None:
    """偏将及以上（clearance>=2）或 human/system 才可写配置。"""
    if p.role not in ("human", "system") and p.clearance < 2:
        raise APIError(ERR_FORBIDDEN,
                       "配置写入需偏将及以上（clearance>=2）", 403)


def require_ontology_admin(p: Principal) -> None:
    """标准域写门禁（S0-2）：本体管理员能力位 且 rank ≥ 偏将，system 旁路。

    双条件缺一不可（UC-S0-4/5/6）：光有能力位没有办案资格不行，
    光有职级没有能力位也不行（改标准 ≠ 办案，PRD §11）。
    ROLE_RANK 与 clearance 是两把尺子（core/access.py 禁止互比），
    这里只比 ROLE_RANK，不碰 clearance。
    """
    if p.role == "system":
        return
    if not p.is_ontology_admin or ROLE_RANK.get(p.role, -1) < ROLE_RANK["偏将"]:
        raise APIError(
            ERR_FORBIDDEN,
            "需要本体管理员权限（且职级 ≥ 偏将）才能修改标准层本体", 403)


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def snapshot_industry(snap_dir: Path) -> str | None:
    """案件快照 pack_meta.json 的 industry 字段（S0-1 三层合并选层依据）。

    与建案拷贝（cases.create_case）同源；缺失/损坏回落 None（E3-1 容错，
    无行业层 = 全域+案件两层装载）。
    """
    meta_path = snap_dir / "pack_meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(
            meta_path.read_text(encoding="utf-8")).get("industry")
    except (json.JSONDecodeError, OSError, AttributeError):
        return None


# ----------------------------------------------------------------------
# 案件级镜头启停（lenses.json，类比规则工坊）
# ----------------------------------------------------------------------
# 镜头是平台级插件（packs/* 自枚举），不属于本体声明——启停文件放
# cases/<cid>/lenses.json（案件快照根，与 state.sqlite 同级），不进本体
# 指纹/归档（改启停 ≠ 改本体版本）；detect 侧读取生效。

LENSES_FILENAME = "lenses.json"


def lens_overrides_path(case_dir: str | Path) -> Path:
    """案件镜头启停文件路径：cases/<cid>/lenses.json。"""
    return Path(case_dir) / LENSES_FILENAME


def load_lens_overrides(case_dir: str | Path) -> dict[str, bool]:
    """读案件镜头启停覆盖 → {skill_id: enabled}；缺失/损坏回落 {}。

    schema：{"schema_version": 1, "lenses": {"<skill_id>": {"enabled": bool}}}
    容错口径同快照 thresholds.json（缺失/非法回落默认）：文件损坏/条目
    非法只忽略该条目，不让配置问题拖垮 BUILD；仅 enabled=false 语义为
    停用（true 为显式确认，不复活包级停用——见 case_batch_lens_ids）。
    """
    path = lens_overrides_path(case_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("lenses")
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for sid, entry in raw.items():
        if isinstance(entry, dict) and isinstance(entry.get("enabled"), bool):
            out[str(sid)] = entry["enabled"]
    return out


def load_lens_canvas_overrides(case_dir: str | Path) -> dict[str, bool]:
    """画布可用开关 → {skill_id: bool}。

    为什么与批量启停拆成两个开关
    ----------------------------
    一个开关管两件事是后续一系列问题的根源：
      - `enabled`        —— 是否在建案/重扫时**自动跑**（产出观察档案）
      - `canvas_enabled` —— 是否在研判画布上**可用**（正兵手动带参跑）

    这是两个不同决策。有的镜头值得自动跑一遍全案，但在具体研判时
    正兵用不上；也有的镜头正兵想随时手动试，但不必每次重扫都跑。

    向后兼容：`canvas_enabled` 缺省时**继承 enabled**（老文件语义不变——
    停用 = 两处都不用）。显式声明后以显式值为准。
    """
    path = lens_overrides_path(case_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("lenses")
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for sid, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        enabled = entry.get("enabled")
        if not isinstance(enabled, bool):
            continue
        canvas = entry.get("canvas_enabled")
        out[str(sid)] = canvas if isinstance(canvas, bool) else enabled
    return out


def copy_layer_dirs(snap_dir: Path, base_dir: Path, tmp_root: Path) -> None:
    """复制数据元上游层到临时校验副本（S0-1 三层合并）：
    _shared 全域层 + _industry/<行业> 行业叠加层。

    objects.json 引用 DE_IDCARD/DE_FIN_ACCT_NO 等上游层数据元，
    缺层则 load_pack 因数据元未注册硬失败。行业由案件快照
    pack_meta.json 的 industry 字段决定（与建案拷贝同源）。
    """
    shared_src = base_dir / "_shared"
    if shared_src.is_dir():
        shutil.copytree(shared_src, tmp_root / "_shared")
    industry = snapshot_industry(snap_dir)
    if industry:
        ind_src = base_dir / "_industry" / industry
        if ind_src.is_dir():
            (tmp_root / "_industry").mkdir(exist_ok=True)
            shutil.copytree(ind_src, tmp_root / "_industry" / industry)


def validate_snapshot(snap_dir: Path, pack_id: str,
                      base_dir: Path) -> None:
    """在临时副本上跑 load_pack 全量校验，不合法抛异常（不落盘）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        copy_layer_dirs(snap_dir, base_dir, tmp_root)
        load_pack(pack_id, base_dir=tmp_root)


def save_config_json(snap_dir: Path, pack_id: str, base_dir: Path,
                     filename: str, data, *, ctx: WebContext,
                     case_id: str, op: str, p: Principal,
                     detail: dict | None = None) -> None:
    """校验通过后原子写配置文件 + 记 ops 审计。

    校验对象是**新内容**：临时副本整包 + 上游层后，把 data 写进副本的
    filename 再过 load_pack 全量强校验（与 etl._write_validated 同范式）——
    只校验旧快照会让悬空引用/不可达收紧漏网（D1 兜底失效）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        copy_layer_dirs(snap_dir, base_dir, tmp_root)
        (tmp_root / pack_id / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        load_pack(pack_id, base_dir=tmp_root)
    atomic_write_json(snap_dir / filename, data)
    ctx.repo.record_ops(op, case_id,
                        {"file": filename, "by": p.operator,
                         **(detail or {})})


def commit_ontology_version(*, repo, cases, case_id: str, snap_dir: Path,
                            op: str, operator: str, reason: str | None = None,
                            changed_files: list[str] | None = None) -> str:
    """本体写落盘后的版本收口（S0-3 F3.1/F3.2，状态机 §9.1）：
    重算指纹 → 版本有变则归档完整快照（R2）+ 回写 case_pack_snapshots +
    追加 pack_snapshot_history（只追加不覆盖）。
    指纹未变（如 knowledge 等非凭据文件写入）不产生历史行，返回当前指纹。
    E3-1：重算/归档失败保留上一版本 + ops 显式留痕，不静默、不抛出
    （配置已落盘生效，版本收口故障不阻断已成功的写响应）。
    """
    try:
        new_fp = cases.fingerprint(snap_dir)
        prev = repo.get_pack_snapshot(case_id)
        prev_fp = prev.version if prev else ""
        if new_fp != prev_fp:
            ref = cases.archive_snapshot(case_id, new_fp)
            repo.update_pack_snapshot_version(case_id, new_fp)
            repo.append_pack_snapshot_history(PackSnapshotHistory(
                case_id=case_id, version=new_fp, prev_version=prev_fp,
                operator=operator, reason=(reason or "").strip(),
                changed_files=list(changed_files or []),
                snapshot_ref=str(ref)))
        return new_fp
    except Exception as e:  # noqa: BLE001
        try:
            repo.record_ops("ontology_commit_failed", case_id,
                            {"op": op, "by": operator,
                             "error": f"{type(e).__name__}: {e}"[:160]})
        except Exception:  # noqa: BLE001
            pass
        return ""


def commit_ontology_write(ctx: WebContext, case_id: str, *, op: str,
                          operator: str, reason: str | None = None,
                          changed_files: list[str] | None = None) -> str:
    """路由侧封装：从 WebContext 解析快照路径后收口本体版本。"""
    pack_id, snap_dir, _base = snapshot_paths(ctx, case_id)
    return commit_ontology_version(
        repo=ctx.repo, cases=ctx.cases, case_id=case_id, snap_dir=snap_dir,
        op=op, operator=operator, reason=reason, changed_files=changed_files)


def record_config_audit(ctx: WebContext, case_id: str, p: Principal, op: str,
                        *, filename: str, reason: str | None = None,
                        summary: dict | None = None) -> None:
    """配置写追加进案件审计链（state.sqlite audit_chain 哈希链；FE-T-012）。

    在配置文件原子落盘后调用：after_state 带 config_action 标记，
    timeline 的 action 派生为 "config"（core.audit._event_action），
    note 列落变更理由；配置整包不进链（体量大、快照文件本身可溯），
    链上只留 谁/何时/哪个文件/改了什么摘要/理由。

    S0-3 版本收口（F3.1/F3.2）：本助手同时是所有配置写路由的统一漏斗，
    在审计前先 commit_ontology_write 重算指纹并追加版本沿革；
    summary 双记 data_version（现状保兼容，D7）与 ontology_version（指纹）。

    审计追加失败不回滚配置（数据已生效），降级为 ops 错误事件留痕，
    避免留痕故障阻断已成功的配置写。

    state.sqlite 写入在 store 层（state_sink，state_store 唯一合法消费面
    之一）；本助手只做版本解析与失败降级编排。
    """
    from server.app.store.state_sink import append_config_event

    ontology_fp = commit_ontology_write(
        ctx, case_id, op=op, operator=p.operator, reason=reason,
        changed_files=[filename])

    try:
        version = ctx.repo.current_version(case_id)
        merged = dict(summary or {})
        merged.setdefault("data_version", version)      # D7：现状保兼容
        if ontology_fp:
            merged.setdefault("ontology_version", ontology_fp)  # D7：双记
        append_config_event(
            state_path=ctx.factory.case_dir(case_id) / "state.sqlite",
            case_id=case_id,
            operator=p.operator,
            op=op,
            filename=filename,
            ontology_version=f"v{version}",
            reason=reason or "",
            summary=merged,
        )
    except Exception as e:  # 留痕失败不阻断主流程
        ctx.repo.record_ops("config_audit_failed", case_id,
                            {"op": op, "file": filename, "by": p.operator,
                             "error": str(e)[:120]})
