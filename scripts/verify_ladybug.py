"""
scripts/verify_ladybug.py
LadybugDB 能力验证（第 1 步）：确认图库可用性与能力边界。

验证项：
  1. 建库 / 连接 / 建节点表 / 建关系表
  2. 节点与边插入
  3. Cypher 基础查询
  4. ★ 多跳 MATCH（Q2 过桥核心能力）
  5. ★ 变长跳 [*1..2]
  6. CSV 批量导入（离线可行路径）
  7. ATTACH DuckDB（需联网下载扩展，离线环境会失败）

关于"两个 duckdb"：
  · Python duckdb 包（pip install duckdb）  —— 独立进程内 OLAP 引擎，本项目 L2/L3 依赖它
  · LadybugDB 的 duckdb 扩展（INSTALL duckdb）—— 让图库能 ATTACH duckdb 文件，需运行时下载

  二者互不相干。前者已内置 parquet/json/core_functions/icu，本项目全部用得到的能力
  无需额外扩展；后者在受限网络下不可得，此时图库改用 CSV 导入（同样可用）。

用法：
    pip install ladybug
    python -m scripts.verify_ladybug
"""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    RESULTS.append((label, ok, note))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  —— {note}" if note else ""))


def main() -> int:
    try:
        import ladybug as lb
    except ImportError:
        print("❌ 未安装 ladybug：pip install ladybug")
        return 1

    print(f"LadybugDB 版本：{getattr(lb, '__version__', '?')}")
    tmp = Path(tempfile.mkdtemp(prefix="lbug_verify_"))

    # 1-3. 建库 / 建表 / 插入 / 基础查询
    try:
        db = lb.Database(str(tmp / "base.lbug"))
        conn = lb.Connection(db)
        conn.execute("CREATE NODE TABLE Company(name STRING, credit STRING, PRIMARY KEY(name))")
        conn.execute("CREATE REL TABLE TRANSFER(FROM Company TO Company, amount INT64)")
        check("建库 + 建节点表 + 建关系表", True)
    except Exception as e:
        check("建库 + 建表", False, str(e).splitlines()[0])
        return _summary()

    try:
        nodes = [("宏业建设", "9133AAA"), ("A建材", "9133BBB"), ("张卫国配偶", "-")]
        with open(tmp / "comp.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "credit"])
            w.writerows(nodes)
        with open(tmp / "edge.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["frm", "to", "amount"])
            w.writerow(["宏业建设", "A建材", 4600000])
            w.writerow(["A建材", "张卫国配偶", 1700000])
        # 用 CSV 批量导入（离线可行路径）
        # Windows 反斜杠路径会被 Cypher parser 当转义序列，COPY 语句必须用正斜杠
        conn.execute(f"COPY Company FROM '{(tmp / 'comp.csv').as_posix()}' (HEADER=true)")
        conn.execute(f"COPY TRANSFER FROM '{(tmp / 'edge.csv').as_posix()}' (HEADER=true)")
        r = conn.execute("MATCH (c:Company) RETURN c.name")
        n = sum(1 for _ in _iter(r))
        check(f"CSV 批量导入 + 基础查询（{n} 节点）", n == 3, "" if n == 3 else f"期望 3 实得 {n}")
    except Exception as e:
        check("CSV 批量导入 + 基础查询", False, str(e).splitlines()[0])

    # 4. 多跳 MATCH（Q2 过桥核心）
    try:
        r = conn.execute(
            "MATCH (a:Company)-[:TRANSFER]->(m:Company)-[:TRANSFER]->(c:Company) "
            "RETURN a.name, m.name, c.name"
        )
        paths = list(_iter(r))
        check(f"多跳 MATCH 两跳过桥（{len(paths)} 条路径）", len(paths) >= 1,
              str(paths[0]) if paths else "")
    except Exception as e:
        check("多跳 MATCH 两跳过桥", False, str(e).splitlines()[0])

    # 5. 变长跳
    try:
        r = conn.execute("MATCH (a:Company {name:'宏业建设'})-[*1..2]->(b) RETURN b.name")
        nb = list(_iter(r))
        check(f"变长跳 [*1..2]（{len(nb)} 个邻居）", len(nb) >= 2,
              str([x[0] for x in nb]))
    except Exception as e:
        check("变长跳 [*1..2]", False, str(e).splitlines()[0])

    # 6. ATTACH DuckDB（需运行时下载扩展）
    #    实测：扩展服务器返回 403 policy_default_denied（服务端策略拒绝，非网络故障）。
    #    且 duckdb 官方扩展源 extensions.duckdb.org 同样 403 → 属出网被统一拦截。
    #    不影响主流程：Python duckdb 包已内置 parquet/json，图库集成走 CSV 导入路径。
    try:
        conn.execute("INSTALL duckdb; LOAD duckdb;")
        check("ATTACH DuckDB 扩展加载", True)
    except Exception as e:
        check("ATTACH DuckDB 扩展加载", False, "需运行时下载扩展，当前环境出网被拦（403）—— 走 CSV 导入路径")

    conn.close()
    return _summary()


def _iter(res):
    while res.has_next():
        yield res.get_next()


def _summary() -> int:
    ok = sum(1 for _, o, _ in RESULTS if o)
    total = len(RESULTS)
    print(f"\n{'=' * 56}")
    print(f"验证结果：{ok}/{total} 项通过")
    failed = [l for l, o, _ in RESULTS if not o]
    if failed:
        print(f"未通过：{failed}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
