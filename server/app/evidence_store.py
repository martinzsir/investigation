"""
server/app/evidence_store.py
REQ-V-009：书证文件存储（cases/<cid>/evidence/，与 uploads/ 数据源物理分离）。

职责边界：
  - 本模块只管文件侧：文件名消毒、sha256、大小上限 20MB、
    存储路径 {case_dir}/evidence/{material_id}/{safe_filename}；
  - 元数据落库走 StateStore.insert_evidence（本模块不碰 state.sqlite）；
  - evidence/ 目录在 ingest 扫描白名单外，天然不参与 BUILD/RESCAN/冷层导入
    （实施方案 REQ-V-009 细节）。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

# 大小上限 20MB（实施方案 REQ-V-009；REQ-V-010 API 层映射 413）
MAX_EVIDENCE_SIZE = 20 * 1024 * 1024

# 控制字符（含 DEL）——文件名一律剔除
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class EvidenceTooLargeError(ValueError):
    """书证超过大小上限（REQ-V-010 API 层捕获映射 413）。"""


def new_material_id() -> str:
    """ev_{uuid12}（DDL 约定）。"""
    return f"ev_{uuid.uuid4().hex[:12]}"


def sanitize_filename(name: str) -> str:
    """存储名消毒：剥两侧路径分隔符取末段（防 ../ 与 ..\\ 穿越）、
    剔控制字符、去首尾空白与点（Windows 尾点/尾空格非法）。
    原始文件名由调用方留 orig_name，本函数结果只作存储名。"""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_CHARS.sub("", name).strip(" .")
    return name or "evidence"


def _upload_bytes(upload) -> tuple[str, bytes]:
    """兼容 (filename, bytes) 元组与 Starlette UploadFile（.filename/.file）。"""
    if isinstance(upload, (tuple, list)) and len(upload) == 2:
        return str(upload[0] or ""), upload[1]
    name = getattr(upload, "filename", None)
    fh = getattr(upload, "file", None)
    if fh is None:
        raise TypeError(
            "upload 须为 (filename, bytes) 或带 .filename/.file 的对象")
    data = fh.read()
    return str(name or ""), data


def save_evidence_file(case_dir, upload) -> dict:
    """书证落 {case_dir}/evidence/{material_id}/{safe_filename}。

    超上限抛 EvidenceTooLargeError（此时不建目录不留文件）；
    成功返回 {material_id, filename(存储名), orig_name, sha256, size, path}——
    前四者 + material_type/note 由调用方（REQ-V-010 API）转 StateStore.insert_evidence。
    """
    orig_name, data = _upload_bytes(upload)
    if len(data) > MAX_EVIDENCE_SIZE:
        raise EvidenceTooLargeError(
            f"书证超过大小上限：{len(data)} > {MAX_EVIDENCE_SIZE} 字节（20MB）")
    material_id = new_material_id()
    safe_name = sanitize_filename(orig_name)
    target_dir = Path(case_dir) / "evidence" / material_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name
    target.write_bytes(data)
    return {
        "material_id": material_id,
        "filename": safe_name,
        "orig_name": orig_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "path": target,
    }
