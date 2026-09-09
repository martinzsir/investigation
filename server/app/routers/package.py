"""
server/app/routers/package.py
W-028/029：案件包导出 / 下载 / 校验 / 导入。

- export：入队 EXPORT 任务（版本压实 + 审计链冻结 + SHA-256 + zip）；
- download：导出完成后流式下载 zip；
- verify：上传包同步校验（7 步），不入队；
- import：校验通过后入队 IMPORT_PACKAGE 任务。

routers 不写 SQL：校验与导出逻辑在 worker/package.py，router 只编排。
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_NOT_FOUND, ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.worker.package import verify_package
from server.app.worker.tasks import TASK_EXPORT, TASK_IMPORT_PACKAGE, enqueue_task

router = APIRouter(tags=["package"])


class ImportIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


@router.post("/cases/{case_id}/package/export")
def export_package(case_id: str,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """W-028：导出案件包（入队 EXPORT 任务）。"""
    _get_owned_case(case_id, p, ctx.cases)
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_EXPORT,
        created_by=p.operator)
    return ok({"task_id": task.id})


@router.get("/packages/{task_id}/download")
def download_package(task_id: str,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """导出完成后下载包 zip。"""
    task = ctx.repo.get_task(task_id)
    if task is None:
        raise APIError(ERR_NOT_FOUND, f"任务不存在：{task_id}", 404)
    if task.task_type != TASK_EXPORT:
        raise APIError(ERR_VALIDATION, f"非导出任务：{task_id}", 400)
    # 权限：只能下载本租户案件的包
    _get_owned_case(task.case_id, p, ctx.cases)
    zip_path = Path(ctx.factory.case_dir(task.case_id)) / "exports" / f"{task_id}.zip"
    if not zip_path.exists():
        raise APIError(ERR_NOT_FOUND, "导出包不存在（任务可能未完成）", 404)
    return FileResponse(
        str(zip_path), media_type="application/zip",
        filename=f"{task.case_id}_package.zip")


@router.post("/packages/verify")
async def verify_uploaded_package(file: UploadFile = File(...),
                                  p: Principal = Depends(get_principal),
                                  ctx: WebContext = Depends(get_ctx)):
    """W-029：上传包同步校验（7 步），不导入。"""
    work = Path(tempfile.mkdtemp(prefix="pkg_verify_"))
    try:
        zip_path = work / file.filename
        with open(zip_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work / "extracted")
        result = verify_package(work / "extracted")
        return ok({
            "ok": result["ok"],
            "errors": result["errors"],
            "chain_ok": result["chain_ok"],
            "file_count": len(result["manifest"].get("files", {})),
            # W-029 七步清单 + 敏感文件名单（前端 StepChecklist/红框）
            "steps": result["steps"],
            "sensitive_files": result["sensitive_files"],
        })
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/packages/import")
async def import_package(file: UploadFile = File(...),
                         case_id: str = "",
                         name: str = "",
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """W-029：校验通过后入队 IMPORT_PACKAGE 任务。"""
    if not case_id:
        raise APIError(ERR_VALIDATION, "缺少 case_id", 400)
    if ctx.repo.get_case(case_id) is not None:
        raise APIError(ERR_VALIDATION, f"案件已存在：{case_id}", 409)
    work = Path(tempfile.mkdtemp(prefix="pkg_import_"))
    try:
        zip_path = work / file.filename
        with open(zip_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        # 预校验
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work / "extracted")
        result = verify_package(work / "extracted")
        if not result["ok"]:
            raise APIError(
                ERR_VALIDATION,
                "案件包校验失败：" + "; ".join(result["errors"]), 422)
        # 入队导入任务（Worker 侧解压实际导入）
        task = enqueue_task(
            ctx.repo, case_id=case_id, task_type=TASK_IMPORT_PACKAGE,
            params={"zip_path": str(zip_path), "case_id": case_id,
                    "name": name or case_id, "tenant_id": p.tenant_id},
            created_by=p.operator)
        return ok({"task_id": task.id})
    except APIError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    # 注意：zip_path 需保留供 Worker 读取，不清理 work
