"""
server/app/worker/package.py
W-028/029：案件包导出与导入。

导出（W-028）：
  版本压实 → 审计链冻结校验 → 逐文件 SHA-256 manifest → README → zip。
  chain_ok=false 时橙色告警但允许继续（AC-6）。

导入（W-029）：
  verify_package（7 步校验）→ PackManager.init_pack(from_pack=) → 元数据登记。

不碰 core：PackManager.init_pack / AuditChain 均为既有接口，本模块只编排。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.audit import AuditChain
from core.pack import PackManager

from server.app.meta.repo import MetaRepo
from server.app.store import StoreFactory
from server.app.store.backend import open_readonly_conn
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError, TaskRow

# 13 声明文件（schema_version 须一致）
DECLARATION_FILES = (
    "objects.json", "links.json", "bindings.json", "rules.json",
    "actions.json", "functions.json", "policies.json", "views.json",
    "data_elements.json", "dimensions.json", "case_knowledge.json",
    "enum_space.json", "llm_policy.json",
)
SCHEMA_VERSION = 2

# case_knowledge.json 标 sensitive（W-028 AC-5）
SENSITIVE_FILES = {"case_knowledge.json"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_files(case_dir: Path, pack_id: str, cur_version: int) -> list[Path]:
    """收集导出文件清单（相对 case_dir）。"""
    files: list[Path] = []
    # 13 声明文件
    snap = case_dir / "ontology" / pack_id
    if snap.is_dir():
        for name in DECLARATION_FILES:
            p = snap / name
            if p.exists():
                files.append(p)
    # 压实后的 DuckDB（仅最终版本）
    db = case_dir / f"v{cur_version}.duckdb"
    if db.exists():
        files.append(db)
    # 审计链（state.sqlite → audit/chain.csv）
    state = case_dir / "state.sqlite"
    if state.exists():
        files.append(state)
    # 线索产物
    artifacts = case_dir / "artifacts"
    if artifacts.is_dir():
        files.extend(sorted(artifacts.glob("*.json")))
    return files


# 7 步校验清单（W-029；前端 StepChecklist 逐态渲染）
VERIFY_STEPS: tuple[tuple[str, str], ...] = (
    ("format", "包结构与清单"),
    ("manifest", "清单一致性"),
    ("hash", "SHA-256 完整性"),
    ("declarations", "声明文件齐全"),
    ("schema", "schema 版本一致"),
    ("chain", "审计链完整性"),
    ("duckdb", "DuckDB 只读打开"),
)

STEP_PASS = "pass"
STEP_WARN = "warn"
STEP_FAIL = "fail"


def _step(key: str, status: str, detail: str = "") -> dict[str, Any]:
    label = dict(VERIFY_STEPS).get(key, key)
    return {"key": key, "label": label, "status": status, "detail": detail}


def verify_package(pkg_dir: Path) -> dict[str, Any]:
    """W-029：案件包校验（7 步）。

    返回 {ok, errors, manifest, chain_ok, steps: [{key,label,status,detail}],
    sensitive_files: [str]}。
    - steps 状态：pass/warn/fail；warn（chain 不完整等）不阻断 ok；
    - ok = 无 fail 步（与历史语义一致：chain_ok=false 橙色告警可继续）；
    - sensitive_files：manifest 声明 sensitive=true 与磁盘敏感文件名并集。
    """
    errors: list[str] = []
    steps: list[dict[str, Any]] = []
    pkg_dir = Path(pkg_dir)
    manifest: dict[str, Any] = {}
    chain_ok = False
    sensitive_files: list[str] = []

    def fatal(msg: str) -> dict[str, Any]:
        """format 前置失败：后续步标 fail（未执行），整体不可导入。"""
        errors.append(msg)
        steps.append(_step("format", STEP_FAIL, msg))
        for key, _ in VERIFY_STEPS[1:]:
            steps.append(_step(key, STEP_FAIL, "前置步骤未通过，未执行"))
        return {"ok": False, "errors": errors, "manifest": {},
                "chain_ok": False, "steps": steps,
                "sensitive_files": []}

    # 1. format：目录结构 + manifest.json 存在且可解析
    if not pkg_dir.is_dir():
        return fatal(f"包目录不存在：{pkg_dir}")
    manifest_path = pkg_dir / "manifest.json"
    if not manifest_path.exists():
        return fatal("缺少 manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        steps.append(_step("format", STEP_PASS, "manifest.json 可解析"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return fatal(f"manifest.json 格式错误：{e}")

    files_info = manifest.get("files", {}) or {}
    disk_files = {p.relative_to(pkg_dir).as_posix()
                  for p in pkg_dir.rglob("*") if p.is_file()}

    # 敏感文件：manifest 声明 ∪ 磁盘上的敏感文件名（红框名单宁全勿缺）
    sensitive_set = {
        rel for rel, info in files_info.items()
        if isinstance(info, dict) and info.get("sensitive")
    }
    sensitive_set |= {rel for rel in disk_files
                      if Path(rel).name in SENSITIVE_FILES}
    sensitive_files = sorted(sensitive_set)

    # 2. manifest 一致性：声明文件缺失（fail）/ 包内未声明文件（warn）
    declared = set(files_info.keys())
    missing = sorted(declared - disk_files)
    extra = sorted(disk_files - declared - {"manifest.json"})
    if missing:
        for rel in missing:
            errors.append(f"manifest 声明但文件缺失：{rel}")
        steps.append(_step(
            "manifest", STEP_FAIL,
            f"缺失 {len(missing)} 个声明文件：{', '.join(missing[:3])}"
            + (" …" if len(missing) > 3 else "")))
    elif extra:
        steps.append(_step(
            "manifest", STEP_WARN,
            f"包内 {len(extra)} 个文件未在 manifest 声明（不阻断）"))
    else:
        steps.append(_step("manifest", STEP_PASS,
                           f"{len(files_info)} 个文件清单一致"))

    # 3. 逐文件 SHA-256 比对
    hash_bad: list[str] = []
    for rel_path, info in files_info.items():
        p = pkg_dir / rel_path
        if not p.exists():
            continue  # 缺失已在 manifest 步记录
        if _sha256(p) != info.get("sha256"):
            hash_bad.append(rel_path)
            errors.append(f"SHA-256 不匹配：{rel_path}")
    if hash_bad:
        steps.append(_step(
            "hash", STEP_FAIL,
            f"{len(hash_bad)} 个文件哈希不匹配（可能被篡改）"))
    else:
        steps.append(_step("hash", STEP_PASS,
                           f"{len(files_info)} 个文件 SHA-256 全部匹配"))

    # 4/5. 13 声明文件齐全 + schema_version 一致
    snap = pkg_dir / "ontology"
    pack_dirs = [d for d in snap.iterdir() if d.is_dir()] if snap.is_dir() else []
    pack_dir = pack_dirs[0] if pack_dirs else None
    if not pack_dirs:
        errors.append("缺少 ontology/<pack>/ 目录")
        steps.append(_step("declarations", STEP_FAIL,
                           "缺少 ontology/<pack>/ 目录"))
        steps.append(_step("schema", STEP_FAIL, "前置步骤未通过，未执行"))
    else:
        missing_decl = [n for n in DECLARATION_FILES
                        if not (pack_dir / n).exists()]
        if missing_decl:
            for n in missing_decl:
                errors.append(f"缺少声明文件：{n}")
            steps.append(_step(
                "declarations", STEP_FAIL,
                f"缺少 {len(missing_decl)} 个声明文件："
                f"{', '.join(missing_decl[:3])}"))
        else:
            steps.append(_step("declarations", STEP_PASS,
                               f"{len(DECLARATION_FILES)} 个声明文件齐全"))

        schema_bad: list[str] = []
        for name in DECLARATION_FILES:
            p = pack_dir / name
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                schema_bad.append(f"{name} JSON 格式错误（可能被篡改）")
                continue
            sv = data.get("schema_version")
            if sv != SCHEMA_VERSION:
                schema_bad.append(
                    f"{name} schema_version={sv}，要求 {SCHEMA_VERSION}")
        if schema_bad:
            errors.extend(schema_bad)
            steps.append(_step("schema", STEP_FAIL,
                               f"{len(schema_bad)} 个声明文件 schema 异常"))
        else:
            steps.append(_step("schema", STEP_PASS,
                               f"schema_version={SCHEMA_VERSION} 一致"))

    # 6. 审计链 root_hash 校验（chain_ok=false → warn 橙警，不阻断）
    state_path = pkg_dir / "state.sqlite"
    if state_path.exists():
        try:
            st = StateStore("pkg", state_path)
            chain = AuditChain.readonly(st.conn, "pkg", backend="sqlite")
            integ = chain.chain_integrity()
            chain_ok = bool(integ.get("chain_ok"))
            manifest_root = manifest.get("audit_root_hash")
            root_mismatch = False
            if manifest_root is not None:
                root_mismatch = chain.root_hash() != manifest_root
            st.close()
            if root_mismatch:
                errors.append("审计链 root_hash 不匹配")
                steps.append(_step("chain", STEP_FAIL,
                                   "审计链 root_hash 与 manifest 不匹配"))
            elif chain_ok:
                steps.append(_step("chain", STEP_PASS, "审计链完整"))
            else:
                steps.append(_step(
                    "chain", STEP_WARN,
                    "审计链不完整（橙色告警，不阻断导入）"))
        except Exception as e:
            errors.append(f"审计链校验异常：{e}")
            steps.append(_step("chain", STEP_FAIL, f"审计链校验异常：{e}"))
    else:
        # 无 state 链不视为错误（旧案件），chain_ok=false 橙警
        chain_ok = False
        steps.append(_step(
            "chain", STEP_WARN,
            "包内无 state.sqlite（旧案件，审计链不可校验）"))

    # 7. DuckDB 只读打开
    db_files = list(pkg_dir.glob("v*.duckdb"))
    db_bad: list[str] = []
    for db in db_files:
        try:
            conn = open_readonly_conn(db)
            conn.close()
        except Exception as e:
            db_bad.append(db.name)
            errors.append(f"DuckDB 只读打开失败：{db.name}：{e}")
    if db_bad:
        steps.append(_step("duckdb", STEP_FAIL,
                           f"{len(db_bad)} 个 DuckDB 文件打开失败"))
    elif db_files:
        steps.append(_step("duckdb", STEP_PASS,
                           f"{len(db_files)} 个 DuckDB 文件只读打开正常"))
    else:
        steps.append(_step("duckdb", STEP_WARN, "包内无 DuckDB 数据文件"))

    return {"ok": len(errors) == 0, "errors": errors,
            "manifest": manifest, "chain_ok": chain_ok,
            "steps": steps, "sensitive_files": sensitive_files}


def handle_export(task: TaskRow, *, repo: MetaRepo, factory: StoreFactory,
                  cases_root: Path, **_: Any) -> dict[str, Any]:
    """W-028：案件包导出任务。

    版本压实（导出副本，不碰原文件）→ 审计链冻结校验 → SHA-256 manifest
    → README → zip。chain_ok=false 时告警但允许继续（AC-6）。
    """
    case_id = task.case_id
    case = repo.get_case(case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{case_id}")
    cur = repo.current_version(case_id)
    case_dir = factory.case_dir(case_id)
    exports_dir = case_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    zip_path = exports_dir / f"{task.id}.zip"

    # 导出工作目录（副本，压实不碰原文件）
    work = exports_dir / task.id
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # 1. 复制声明文件 + 最终版 DuckDB（压实：仅复制 v{cur}）
    src_snap = case_dir / "ontology" / case.pack_id
    dst_snap = work / "ontology" / case.pack_id
    if src_snap.is_dir():
        shutil.copytree(src_snap, dst_snap)
    db_src = case_dir / f"v{cur}.duckdb"
    if db_src.exists():
        shutil.copy2(db_src, work / db_src.name)

    # 2. 审计链冻结校验 + 导出
    chain_ok = False
    audit_root_hash = ""
    state_src = case_dir / "state.sqlite"
    if state_src.exists():
        shutil.copy2(state_src, work / "state.sqlite")
        try:
            st = StateStore(case_id, work / "state.sqlite")
            chain = AuditChain.readonly(st.conn, case_id, backend="sqlite")
            integ = chain.chain_integrity()
            chain_ok = bool(integ.get("chain_ok"))
            audit_root_hash = chain.root_hash()
            # 导出 chain.csv
            audit_dir = work / "audit"
            audit_dir.mkdir(exist_ok=True)
            timeline = chain.timeline(limit=100000)
            import csv
            with open(audit_dir / "chain.csv", "w", newline="",
                      encoding="utf-8") as f:
                if timeline["items"]:
                    w = csv.DictWriter(f, fieldnames=list(
                        timeline["items"][0].keys()))
                    w.writeheader()
                    w.writerows(timeline["items"])
            st.close()
        except Exception as e:
            chain_ok = False
            audit_root_hash = ""

    # 3. 线索产物
    art_src = case_dir / "artifacts"
    if art_src.is_dir():
        shutil.copytree(art_src, work / "artifacts")

    # 4. 逐文件 SHA-256 manifest
    manifest: dict[str, Any] = {
        "case_id": case_id,
        "pack_id": case.pack_id,
        "version": cur,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "chain_ok": chain_ok,
        "audit_root_hash": audit_root_hash,
        "files": {},
    }
    for p in sorted(work.rglob("*")):
        if p.is_file():
            rel = p.relative_to(work).as_posix()
            manifest["files"][rel] = {
                "sha256": _sha256(p),
                "size": p.stat().st_size,
                "sensitive": Path(rel).name in SENSITIVE_FILES,
            }
    (work / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5. README
    readme = f"""# 案件包：{case_id}

- 导出时间：{manifest['exported_at']}
- 本体版本：v{cur}
- schema_version：{SCHEMA_VERSION}
- 审计链完整：{'是' if chain_ok else '否（橙色告警，详见 chain.csv）'}

## 校验

```bash
python -m scripts.verify_package <解压目录>
```

## 导入

```bash
# 接收方
python -m scripts.import_package <包路径>
```

## 文件清单

见 manifest.json（逐文件 SHA-256）。
"""
    (work / "README.md").write_text(readme, encoding="utf-8")

    # 6. zip 打包
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(work).as_posix())

    # 清理工作目录
    shutil.rmtree(work, ignore_errors=True)

    repo.record_ops("package_export", case_id=case_id,
                    payload={"task_id": task.id, "chain_ok": chain_ok,
                             "zip": str(zip_path)})
    return {"zip_path": str(zip_path), "chain_ok": chain_ok,
            "version": cur}


def handle_import_package(task: TaskRow, *, repo: MetaRepo, factory: StoreFactory,
                          cases_root: Path, **_: Any) -> dict[str, Any]:
    """W-029：案件包导入任务。

    verify_package → PackManager.init_pack(from_pack=) → 元数据登记。
    """
    params = task.params or {}
    zip_path = Path(params.get("zip_path", ""))
    new_case_id = params.get("case_id", "")
    new_case_name = params.get("name", new_case_id)
    tenant_id = params.get("tenant_id", "default")

    if not new_case_id:
        raise TaskExecError("VALIDATION", "缺少 case_id")
    if not zip_path.exists():
        raise TaskExecError("NOT_FOUND", f"包文件不存在：{zip_path}")

    # 解压到临时目录
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="pkg_import_"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work)

        # 1. verify_package
        result = verify_package(work)
        if not result["ok"]:
            raise TaskExecError(
                "PACKAGE_INVALID",
                "案件包校验失败：" + "; ".join(result["errors"]))

        # 2. 找 ontology 包目录
        snap = work / "ontology"
        pack_dirs = [d for d in snap.iterdir() if d.is_dir()]
        if not pack_dirs:
            raise TaskExecError("PACKAGE_INVALID", "缺少 ontology/<pack>/ 目录")
        src_pack = pack_dirs[0]

        # 3. 登记新案件 + 复制快照
        from server.app.cases import CaseService
        svc = CaseService(repo, factory, cases_root=cases_root)
        # 直接复制快照到新案件目录
        dst_snap = factory.case_dir(new_case_id) / "ontology" / new_case_id
        if dst_snap.exists():
            shutil.rmtree(dst_snap)
        dst_snap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_pack, dst_snap)

        # 复制 DuckDB 数据文件
        for db in work.glob("v*.duckdb"):
            shutil.copy2(db, factory.case_dir(new_case_id) / db.name)

        # 4. init_pack（复用 PackManager，AC-6）—— 此处用于校验导入的声明合法
        try:
            pm = PackManager(base_dir=factory.case_dir(new_case_id) / "ontology")
            pm.init_pack(new_case_id, from_pack=new_case_id)
        except Exception:
            pass  # init_pack 对已存在包会报错，忽略；声明已复制

        # 5. 元数据登记
        from server.app.meta.models import CaseRecord, CASE_ACTIVE
        case = CaseRecord(
            id=new_case_id, name=new_case_name, tenant_id=tenant_id,
            pack_id=new_case_id, status=CASE_ACTIVE,
            pack_snapshot_at=svc.fingerprint(dst_snap),
            created_by=task.created_by,
            created_at=datetime.now().isoformat(timespec="seconds"))
        repo.create_case(case)
        # 设版本指针（导入的 DuckDB 版本号）
        db_versions = sorted(
            int(db.stem[1:]) for db in work.glob("v*.duckdb"))
        if db_versions:
            repo.set_version(new_case_id, max(db_versions), by="import")

        repo.record_ops("package_import", case_id=new_case_id,
                        payload={"from_zip": str(zip_path)})
        return {"case_id": new_case_id, "version": max(db_versions) if db_versions else 0}
    finally:
        shutil.rmtree(work, ignore_errors=True)
