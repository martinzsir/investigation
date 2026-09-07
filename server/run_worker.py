"""
server/run_worker.py
Worker 进程入口（W-006）：轮询 tasks 表，认领并执行 BUILD 等后台任务。

用法（Windows/WSL 均可，纯 Python）：
  python -m server.run_worker --workers 2 \
      --meta meta/meta.db --cases cases \
      [--ontology ontology] [--template investigation.duckdb]
  python -m server.run_worker --once   # 单步执行一条任务后退出（运维/自测）

与 API 进程分离部署：API 只入队（W-006），本进程才执行构建。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.app.cases import CaseService  # noqa: E402
from server.app.meta.repo_sqlite import SqliteMetaRepo  # noqa: E402
from server.app.store import StoreFactory  # noqa: E402
from server.app.worker.pool import WorkerPool  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="孙武 Worker（案件构建后台任务）")
    ap.add_argument("--meta", default="meta/meta.db", help="元数据 SQLite 路径")
    ap.add_argument("--cases", default="cases", help="案件根目录")
    ap.add_argument("--ontology", default=None,
                    help="ontology 根（缺省平台 ontology/）")
    ap.add_argument("--template", default=None,
                    help="v1 模板库路径（如 investigation.duckdb）")
    ap.add_argument("--workers", type=int, default=2, help="全局并发数")
    ap.add_argument("--once", action="store_true",
                    help="单步执行一条任务后退出")
    args = ap.parse_args(argv)

    repo = SqliteMetaRepo(args.meta)
    factory = StoreFactory(cases_root=args.cases, meta=repo)
    svc = CaseService(repo, factory, ontology_root=args.ontology,
                      cases_root=args.cases)
    ctx: dict = {"factory": factory,
                 "snapshot_base_for": svc.snapshot_ontology_root}
    if args.template:
        ctx["template_db"] = args.template

    pool = WorkerPool(repo, ctx, max_workers=args.workers)
    if args.once:
        ran = pool.run_next()
        print("[worker] --once:", "executed one task" if ran else "no task")
        return 0

    pool.start()
    print(f"[worker] started workers={args.workers} meta={args.meta} "
          f"cases={args.cases} (Ctrl+C 停止)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[worker] stopping...")
        pool.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
