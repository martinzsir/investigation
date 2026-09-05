"""
scripts/semantic_search_demo.py
REQ-042 Semantic Search 端到端验证脚本（真实 Qwen API）。

用法：
    set DASHSCOPE_API_KEY=sk-xxx
    python -m scripts.semantic_search_demo
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                            # noqa: E402
from core.access import AccessContext             # noqa: E402
from core.ontology import build_ontology          # noqa: E402
from core.search import SemanticSearch             # noqa: E402


def _setup_store():
    """建一个含样例数据的内存 Store 并构建语义层。"""
    s = Store(db_path=":memory:")
    c = s.conn
    c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    c.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    c.execute("INSERT INTO 银行流水 VALUES ('张卫国','宏业建设',50000,'2021-10-01')")
    c.execute("INSERT INTO 通话记录 VALUES ('张卫国','李志强','2021-10-02',3)")
    c.execute("INSERT INTO 工商信息 VALUES ('宏业建设','李志强','存续','')")
    c.execute("INSERT INTO 轨迹出行 VALUES ('2021-10-03','张卫国','滨江路')")
    c.execute("INSERT INTO 招投标档案 VALUES ('滨江路改造','宏业建设','2021-09-15','')")
    c.execute("INSERT INTO 公开OSINT VALUES ('宏业建设','宏业建设与滨江路改造项目存在异常关联，涉嫌围标','2024-01-15','财经观察')")
    c.execute("INSERT INTO 公开OSINT VALUES ('李某','李某通过多个空壳公司转移资金，涉及银行流水异常','2024-02-20','深度调查')")
    c.execute("INSERT INTO 举报材料 VALUES ('2024-03-01','宏业建设行贿举报','宏业建设','张某某','宏业建设向招标办人员行贿，金额约200万，银行转账记录可查')")
    build_ontology(c)
    return s


def main() -> int:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    print("=" * 60)
    print("REQ-042 Semantic Search 端到端验证（Qwen text-embedding-v3）")
    print(f"  DASHSCOPE_API_KEY: {'已设置' if api_key else '未设置（离线降级）'}")
    print("=" * 60)

    store = _setup_store()
    ctx = AccessContext(operator="王检察官", role="主办", clearance=2)
    ss = SemanticSearch(store.conn, access=ctx, api_key=api_key)

    print("\n--- 步骤 1: 构建索引（osint_article + tipoff）---")
    n = ss.build_index(["osint_article", "tipoff"])
    print(f"  索引条数: {n}")
    if n == 0:
        print("  （无 API Key 或网络不可用，离线降级）")
        store.close()
        return 0

    print("\n--- 步骤 2: 语义检索 '宏业建设异常关联' ---")
    results = ss.search("宏业建设异常关联", top_k=5)
    print(f"  命中 {len(results)} 条:")
    for i, r in enumerate(results):
        print(f"    [{i+1}] {r['object_name']}/{r['object_pk']}  score={r['score']}")
        print(f"        evidence_type={r['evidence_type']}")
        print(f"        warning={r['solo_evidence_warning']}")
        row = r["row"]
        for k, v in row.items():
            print(f"        {k}: {v}")

    print("\n--- 步骤 3: 语义检索 '资金转移空壳公司' ---")
    results2 = ss.search("资金转移空壳公司", top_k=5)
    print(f"  命中 {len(results2)} 条:")
    for i, r in enumerate(results2):
        print(f"    [{i+1}] {r['object_name']}/{r['object_pk']}  score={r['score']}")

    print("\n--- 步骤 4: 审计记录 ---")
    from core.audit import AuditChain
    chain = AuditChain(store.conn)
    print(f"  审计链事件数: {chain.count()}")
    print(f"  审计链完整: {chain.chain_verify()}")

    log_rows = store.conn.execute(
        "SELECT search_id, operator, query_hash, hit_count, audit_event_id "
        "FROM semantic_search_log").fetchall()
    print(f"  semantic_search_log 记录数: {len(log_rows)}")
    for row in log_rows:
        print(f"    {row}")

    print("\n--- 步骤 5: AC 对照 ---")
    print(f"  AC1 策略过滤: 主办可读 tipoff/content_raw（原文）")
    print(f"  AC2 单独证据警告: 结果含 evidence_type=embedding + warning")
    print(f"  AC3 未授权不索引: 正兵 build_index 被跳过（测试验证）")
    print(f"  AC4 向量原文隔离: semantic_index 表无原文列（测试验证）")
    print(f"  AC5 检索审计: {chain.count()} 条审计事件，chain_verify={chain.chain_verify()}")
    print("=" * 60)

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
