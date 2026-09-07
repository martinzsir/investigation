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
from server.app.worker.orphan_scan import scan_once  # noqa: E402
from server.app.worker.pool import WorkerPool  # noqa: E402
from server.app.worker.reclaim import VersionReclaimer  # noqa: E402


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
    ap.add_argument("--reclaim-interval", type=float, default=60.0,
                    help="版本回收/孤儿扫描周期（秒，W-008/009）")
    ap.add_argument("--orphan-ttl-days", type=float, default=7.0,
                    help="孤儿隔离区保留天数（默认 7，W-009 AC-3）")
    ap.add_argument("--orphan-dry-run", action="store_true",
                    help="孤儿扫描只记 ops_events 不移动文件")
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

    # W-009：启动时先跑一轮孤儿扫描（异常不阻塞 Worker 主流程）
    try:
        scan_stats = scan_once(repo, factory, dry_run=args.orphan_dry_run,
                               ttl_days=args.orphan_ttl_days)
        print(f"[worker] orphan scan: cases={scan_stats['scanned_cases']} "
              f"orphans={len(scan_stats['orphans'])} "
              f"quarantined={scan_stats['quarantined']} "
              f"cleaned={scan_stats['cleaned']}")
    except Exception as e:  # noqa: BLE001
        print(f"[worker] orphan scan failed (ignored): {type(e).__name__}: {e}")

    # W-008：版本延迟回收器（常驻，与任务池并行）
    reclaimer = VersionReclaimer(repo, factory,
                                 interval=args.reclaim_interval)
    reclaimer.start()

    pool.start()
    print(f"[worker] started workers={args.workers} meta={args.meta} "
          f"cases={args.cases} reclaim_interval={args.reclaim_interval}s "
          f"(Ctrl+C 停止)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[worker] stopping...")
        reclaimer.stop()
        pool.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
