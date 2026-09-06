"""
run_all.py —— 「孙武侦查官」单一入口（任务 ① 交付）

完整管线（single source of truth）：
  1. 生成模拟数据（data.gen_sim）
  2. 初始化 DuckDB（L2 温层 + 冷层视图 + Q1 物化）
  3. ★ 数据接入适配层：任意格式 → 统一 schema（此处模拟为多格式样本）
  4. ★ 实体对齐（人名 + 组织层级）→ canonical 映射
  5. ★ 人工确认工作台：needs_review 候选推给正兵，accept/reject/defer
  6. 采样预演：1% 采样验证假设方向，再决定全量
  7. 庙算 → 知己 → 虚实/奇正/用间（skill_invoke 驱动）
  8. 血缘去重合并 + 优先级排序 + 用间交叉升格
  9. 处置状态（状态机 + 审计链 + DuckDB 持久化）
 10. 导出操作台数据（HTML 渲染用）

侦查逻辑（庙算→知己→虚实→奇正→用间→全胜）一行未改，
仅底层执行底座随数据量从「分布式集群」裁剪为「DuckDB + LadybugDB 单机栈」。

用法：
    python run_all.py                # 全量跑（含 CLI 确认工作台，用 --yes 自动 accept 演示）
    python run_all.py --auto-review # 自动演示：所有候选直接 accept（仅演示，生产须人工）
    python run_all.py --no-cli      # 跳过交互式 CLI（CI/测试用）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
# 注：不再把 ROOT.parent 加入 sys.path —— workspace 根存在同名 skills 包会遮蔽本包 skills。
# core.entity 已改为按绝对路径加载 entity_resolution，无需依赖 sys.path。

from core import Store, skill_invoke, get_registry, lineage, review, record_dimension_gaps
from core.entity import run_entity_resolution, apply_org_to_duckdb
from core.run_health import RunHealth
from skills.registry_bootstrap import register_all


def step(title: str):
    print(f"\n{'='*60}\n>>> {title}\n{'='*60}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-review", action="store_true", help="自动演示 accept（仅演示用）")
    ap.add_argument("--no-cli", action="store_true", help="跳过交互式 CLI")
    ap.add_argument("--operator", default="王检察官", help="确认工作台具名操作者")
    args = ap.parse_args()

    # 0. 数据准备（沿用既有脚本）
    step("0. 数据准备：生成模拟数据 + 初始化 DuckDB")
    subprocess.run([sys.executable, "-m", "data.gen_sim"], cwd=ROOT, check=False, capture_output=True)
    subprocess.run([sys.executable, "-m", "scripts.init_duckdb"], cwd=ROOT, check=False, capture_output=True)

    store = Store()
    register_all()

    # REQ-G-010：统一降级/健康度层。全管线"失败被表达成数据"的统一收口——
    # 规则零命中、函数空转、事件发布失败、实体跳过、版本锚缺失、派发失败等落 run_diagnostic，
    # 产物首部"健康度"小节呈现；None 兼容红线（此处显式启用）。
    health = RunHealth(store.conn)

    # ===== 3. 数据接入适配层（任务①：多格式 → 统一 schema）=====
    step("3. 数据接入适配层：模拟多格式样本 → 统一 schema")
    from data_ingest import DataIngestManager   # 见下文 data_ingest.py
    ingest = DataIngestManager(store)
    sample_dir = ROOT / "data" / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample_files = _prepare_sample_files(sample_dir)   # CSV/Excel/JSON/SQLite/Parquet 各一份
    ingest_records = ingest.ingest_directory(sample_dir)
    ok_records = [r for r in ingest_records if r.get("status") == "success"]
    fail_records = [r for r in ingest_records if r.get("status") != "success"]
    print(f"  接入 {len(ok_records)}/{len(ingest_records)} 个文件成功，统一 schema 后写入 DuckDB")
    for r in ingest_records:
        flag = "✅" if r.get("status") == "success" else "❌"
        extra = f"  error={r.get('error')}" if r.get("status") != "success" else ""
        print(f"    {flag} {r.get('format', '?'):8s} {r.get('source_type', '?'):12s} "
              f"{r.get('rows', 0):>5} 行  {Path(r['file']).name}{extra}")

    # ===== 4. 实体对齐：人名 + 组织层级 =====
    step("4. 实体对齐：人名 + 组织层级归并")
    result = run_entity_resolution(
        store,
        alias_dict={"宏业建设": ["宏业建设（集团）", "宏业建设有限公司"]},
        org_alias_dict={"宏业建设": ["宏业建设第一项目部", "宏业建设（集团）"]},
        health=health,
    )
    person = result["person"]
    org = result["org"]
    print(f"  人名：{person.report()['total_records']} 记录 → {person.report()['total_entities']} 实体 "
          f"(强合并 {len(person.report()['strong_merges'])} 组)")
    print(f"  组织：{org.report()['total_records']} 记录 → {org.report()['total_org_entities']} 法人 "
          f"(强合并 {len(org.report()['strong_merges'])} 组)")
    print(f"  待正兵确认候选：{len(person.review_candidates()) + len(org.review_candidates())} 条")

    # 组织层级回写：业务表新增 canonical_org_* 列
    apply_org_to_duckdb(store, org,
                        tables=["银行流水", "招投标档案"],
                        name_columns=["主体", "对方"],
                        health=health)

    # ===== 5. 人工确认工作台 =====
    step("5. 人工确认工作台：needs_review 候选 → accept / reject / defer")
    queue = review.ReviewQueue.from_resolvers(person_resolver=person, org_resolver=org)
    print(f"  队列共 {len(queue)} 条，其中待确认 {len(queue.pending())} 条")

    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)

    if args.auto_review:
        # 演示模式：全部 accept（生产环境此处应走 HTML 操作台人工逐条确认）
        for d in queue.pending():
            queue.accept(d.candidate_id, operator=args.operator, reason="演示模式自动确认")
        print(f"  [演示] 已自动 accept {len(queue.pending())} 条")
    elif not args.no_cli:
        queue.run_cli(auto_operator=args.operator)

    # 把正兵确认结果写回：只有 ACCEPTED 才进正式 mapping
    accepted = queue.accepted_mapping()
    print(f"  正式合并映射（仅 accepted）：{len(accepted)} 条")
    # 拒绝的变体：从 person mapping 中移除，确保不误合并
    final_person_mapping = dict(person.mapping())
    for d in queue.decided():
        if d.status == review.Decision.REJECTED:
            for v in d.variants:
                final_person_mapping.pop(v, None)
    queue.to_json(str(out_dir / "review_queue.json"))

    # REQ-016：人审决策落表——accept → entity_mapping 受保护表（6.5 构建语义层时归并生效）；
    # reject/defer 只写 review.decided(feedback) 事件，不删证据、不触发重建。
    from core.review_loop import (
        ensure_tables as _ensure_review_tables,
        record_accept as _record_accept,
        record_feedback as _record_feedback,
    )
    _ensure_review_tables(store.conn)
    _map_n = 0
    _fb_n = 0
    for _d in queue.decided():
        if _d.status == review.Decision.ACCEPTED:
            _map_n += _record_accept(store.conn, _d)
        else:
            _record_feedback(store.conn, _d)
            _fb_n += 1
    print(f"  人审决策落表：entity_mapping {_map_n} 条归并，feedback 事件 {_fb_n} 条"
          f"（语义层构建时归并生效，重建不清除）")

    # ===== 6. 采样预演 =====
    step("6. 采样预演：1% 采样验证假设方向 → 决定全量")
    from core.sampling import SamplingPreflight
    preflight = SamplingPreflight(store, sample_ratio=0.01)
    pre = preflight.run(["H1", "H4"])   # 对 H1(季度末整数现金存入)/H4(过桥) 做预演
    for r in pre["results"]:
        print(f"  [{r['hypothesis_id']}] 采样 {r['sampled_rows']} 行，命中 {r['hit_rows']} 行 "
              f"→ 命中率 {r['hit_rate']:.2%} → 判定：{r['verdict']}")
    print(f"  整体判定：{pre['overall_verdict']}（{pre['suggest']}）")
    if pre["overall_verdict"] == "方向否定":
        print("  ⚠ 方向否定，停止全量扫描（演示中仍继续以展示完整管线）")

    # ===== 6.5 语义层（Palantir Ontology 风格：Object/Link 语义表）=====
    step("6.5 语义层构建：obj_*/lnk_* 语义表（声明式编译，代理键幂等）")
    from core.ontology import build_ontology
    ontology_stats = build_ontology(store.conn)
    for name, n in ontology_stats["objects"].items():
        print(f"  obj_{name:<12} {n} 行")
    for name, n in ontology_stats["links"].items():
        print(f"  lnk_{name:<12} {n} 行")
    for s in ontology_stats["skipped"]:
        print(f"  ⚠ 跳过 {s}")
    from core.run_health import (record_build_dirty, record_build_degraded,
                                 record_build_quarantine)
    n_dirty = record_build_dirty(store, ontology_stats, run_id=health.run_id)
    if n_dirty:
        print(f"  ⚠ TRY_CAST 脏值降级 {n_dirty} 项（已置 NULL，诊断 kind=source_value_cast_failed）")
        for d in ontology_stats["dirty"]:
            print(f"    · {d}")
    n_deg = record_build_degraded(store, ontology_stats, run_id=health.run_id)
    if n_deg:
        print(f"  ⚠ 可选源列缺失降级 {n_deg} 项（已置类型化 NULL，诊断 kind=source_column_missing）")
        for d in ontology_stats["degraded"]:
            print(f"    · {d}")
    n_qua = record_build_quarantine(store, ontology_stats, run_id=health.run_id)
    if n_qua:
        print(f"  ⚠ CAST 失败隔离 {n_qua} 项（整行剔出语义层，落 build_quarantine，"
              f"诊断 kind=source_value_quarantined）")
        for d in ontology_stats["quarantine"]:
            print(f"    · {d.get('object')}.{d.get('property')}<-{d.get('column')}: "
                  f"{d.get('quarantined_rows')} 行已隔离")
    # REQ-D-008：清洗剔除统计落健康度（剔除率 >30% 升 warning，样本已脱敏）
    from core.run_health import record_clean_stats
    n_clean = record_clean_stats(store, ontology_stats, run_id=health.run_id)
    if n_clean:
        print(f"  ⚠ 清洗剔除 {n_clean} 个属性有剔除落账（诊断 kind=clean_drop_rate）")
    # REQ-D-015：业务键去重冲突规模落健康度（null_policy 空值剔除已含在 clean_stats/quarantine）
    from core.run_health import record_dedup_conflicts
    n_dd = record_dedup_conflicts(store, ontology_stats, run_id=health.run_id)
    if n_dd:
        print(f"  ⚠ 业务键去重冲突 {n_dd} 项（按业务键收敛，诊断 kind=dedup_key_conflict）")

    # ===== 6.6 构建后质量门：合规/敏感/新鲜度/单位（REQ-D-016/018/019/020）=====
    # 只告警不阻断：违规/疑似落 run_diagnostic，绝不抛异常中断侦查管线。
    step("6.6 构建后质量门：合规（016）+ 敏感列（018）+ 数据新鲜度（019）+ 单位一致性（020）")
    try:
        from core.gateway import OntologyReadGateway
        from core import compliance, sensitive_scan, data_freshness, unit_scan
        gw = OntologyReadGateway(store.conn)
        comp = compliance.scan(gw, health=health)
        if comp["violations"]:
            print(f"  ⚠ 合规违规 {comp['violations']} 处"
                  f"（覆盖 {comp['scanned']} 属性，诊断 kind=compliance_violation）")
        else:
            print(f"  · 合规扫描：{comp['scanned']} 属性零违规")
        sens = sensitive_scan.scan(gw, health=health)
        if sens["suspects"]:
            print(f"  ⚠ 疑似敏感列未声明遮蔽 {sens['suspects']} 个"
                  f"（诊断 kind=sensitive_column_suspect，只告警不阻断）")
        else:
            print(f"  · 敏感列扫描：{sens['scanned']} 属性无疑似")
        fresh = data_freshness.scan(gw, health=health)
        if fresh["stale"]:
            print(f"  ⚠ 数据时间超期 {fresh['stale']} 个对象属性"
                  f"（诊断 kind=data_freshness_stale，与本体版本新鲜度分开）")
        else:
            print(f"  · 数据新鲜度：{len(fresh['objects'])} 个时间属性均在阈值内")
        unit = unit_scan.scan(gw, health=health)
        if unit["mismatches"]:
            print(f"  ⚠ 单位/量级疑似混用 {len(unit['mismatches'])} 组"
                  f"（诊断 kind=unit_mismatch，只提示不定性）")
        if unit["missing_unit"]:
            print(f"  · 单位声明缺失提示 {len(unit['missing_unit'])} 个金额属性（info，非硬失败）")
        if not unit["mismatches"] and not unit["missing_unit"]:
            print(f"  · 单位一致性：{len(unit['groups'])} 组金额数据元无疑似")
    except Exception as e:   # 质量门自身故障不阻断侦查（诊断层同理）
        print(f"  ⚠ 质量门执行异常（不阻断）：{e}")
        health.record("quality_gate_failed", "warning", source="quality_gate",
                      reason=f"构建后质量门执行异常（不阻断）：{e}")

    # ===== 7-8. 侦查主流程（skill_invoke 驱动）=====
    step("7-8. 侦查主流程：庙算→知己→虚实/奇正/用间 + 血缘去重 + 优先级")
    registry = get_registry()
    ctx = {
        "可用数据": ["银行流水", "通话记录", "招投标档案", "工商信息", "轨迹出行", "公开OSINT", "举报材料"],
        "未调取": ["房产车辆"],
        "证据缺口": ["现金来源无法溯源", "A公司流水缺口95万"],
        "授权边界": ["不可查房产车辆", "不可直接接触对象"],
    }
    miao = _build_miaosuan(store, ctx, health=health)

    all_clues: list = []
    for sid in ["xu_shi", "qi_zheng", "yong_jian"]:
        clues = skill_invoke(registry, sid, miao=miao, store=store, ctx=ctx,
                             health=health)
        print(f"  [{sid}] 产出 {len(clues)} 条 LineageClue")
        all_clues.extend(clues)

    merged = lineage.dedupe_and_merge(all_clues, threshold=0.5)
    print(f"  血缘去重：{len(all_clues)} → {len(merged)} 条")
    # 注意：prioritize_clues 返回排序后的新列表，必须接收返回值。
    # 此前写成裸调用，merged 仍是原顺序 → 导出的 priority_rank 与列表顺序对不上。
    merged = lineage.prioritize_clues(merged)
    top3 = [(c.detail.get("priority_rank"), c.detail.get("priority_score"), c.title)
            for c in merged[:3]]
    print(f"  优先级 TOP3：{top3}")
    report = lineage.lineage_report(merged)
    print(f"  用间覆盖：{report['jian_coverage']} → 交叉等级：{report['cross_level']}")

    # ===== 8b. 图库两跳过桥（L4，Cypher + SQL 双轨）=====
    # 可选组件：ladybug 未安装时自动降级，仅保留 SQL 轨，不阻断管线。
    step("8b. 图库两跳过桥：Cypher 多跳 + SQL 自连接 双轨比对")
    graph_report = _run_graph_overpass(store)
    if graph_report:
        report["graph_overpass"] = graph_report

    # ===== 9. 处置状态 =====
    step("9. 处置状态：状态机 + 审计链 + 持久化")
    from core.disposal import DisposalBoard
    board = DisposalBoard(merged, store=store, health=health)
    board.restore()
    if merged:
        top = merged[0]
        board.transition(top.clue_id, "查证中", operator=args.operator, note="已调取流水核实")
        board.confirm(top.clue_id, operator=args.operator, note="过桥结构经比对成立")
        try:
            board.file(top.clue_id, operator=args.operator, legal_basis="杭检立〔2026〕XX号")
            print(f"  {top.clue_id} → 已立案（法定依据入审计链）")
        except ValueError as e:
            print(f"  [预期拦截] {e}")
    for c in merged[1:]:
        board.transition(c.clue_id, "查证中", operator="system")
    written = board.persist()
    # obj_clue 是处置快照：persist 后重建语义层，避免语义表停留旧状态
    from core.ontology import build_ontology as _rebuild_ontology
    _rebuild_ontology(store.conn)
    print(f"  处置状态落 DuckDB：{written} 行（语义层 obj_clue 已同步刷新）")
    board.print_report()

    # ===== 10. 导出操作台数据 =====
    step("10. 导出操作台数据")
    # 必须在此重新生成 report：上面第 9 步改变了处置状态，
    # 若沿用第 7-8 步的旧 report，by_status 会停留在『全部待查』，与 DuckDB 真实状态矛盾。
    report = lineage.lineage_report(merged)
    # 庙算覆盖完整性报告（维度/数据源/间类/冲突/枚举候补）——先于健康度小节计算，
    # 维度覆盖缺口（G-008/009 双轨口径）落 coverage_gap 诊断，进入"健康度"。
    miao_cov = miao.report(ctx["可用数据"])
    _dc = miao_cov.get("dimension_coverage", {})
    # REQ-G-024：声明缺口与实证缺口独立留痕——实证缺口不再被声明轨 alarm 门控，
    # 声明 100% 但实证缺维时健康度不得报 healthy；source 区分补救动作
    # （miaosuan:dimension=补假设；miaosuan:dimension:empirical=补数据/查检测器）。
    record_dimension_gaps(_dc, health)
    # REQ-G-017：规则推翻率超阈告警；REQ-G-018：审计链完备性自检（断链/缺字段/缺号）。
    # 二者均在健康度小节生成前完成留痕。
    try:
        from core.metrics import alert_override_rate
        alert_override_rate(store.conn, health=health)
    except Exception as e:  # 自检本身不得炸管线
        health.record("function_empty_degraded", "info",
                      source="metrics:override_rate",
                      reason=f"推翻率告警自检跳过：{str(e)[:80]}")
    try:
        from core.audit import AuditChain
        audit_integrity = AuditChain(store.conn, health=health).chain_integrity()
    except Exception as e:
        audit_integrity = {"chain_ok": False, "error": str(e)[:120]}
        health.record("audit_integrity_gap", "warning", source="audit:chain",
                      reason=f"审计链自检异常：{str(e)[:80]}")
    # REQ-G-019：异常线索通道——把零命中疑似失效/函数空转/覆盖缺口转成待核实条目。
    # 与正常线索同构但 is_anomaly=true、级别恒待核实；**绝不**并入 findings/merged，
    # 不参与五间交叉升格（交叉只数 obj_*/lnk_* 语义表）。独立产物键 + 审计留痕。
    anomaly_clues: list = []
    try:
        from core.anomaly_channel import emit_anomaly_clues
        anomaly_clues = emit_anomaly_clues(health)  # 落 anomaly_clue_emitted 诊断
    except Exception as e:  # 异常通道自身不得炸管线
        health.record("function_empty_degraded", "info",
                      source="anomaly_channel",
                      reason=f"异常线索生成跳过：{str(e)[:80]}")
    # REQ-G-010：健康度小节置于所有结论字段之前（运行留痕，不参与线索升格/定性）。
    report = {"健康度": health.health_section(), **report}
    # 重新生成会丢掉 8b 挂上的图库段结果，需补回
    if graph_report:
        report["graph_overpass"] = graph_report
    report["miao_coverage"] = miao_cov
    report["审计链完备性"] = audit_integrity
    if anomaly_clues:
        report["异常线索_待核实"] = [
            {k: c.get(k) for k in ("rule_id", "候选虚处", "依据", "级别",
                                   "subject", "anomaly_kinds", "anomaly_count",
                                   "diagnostic_ids", "dimension")}
            for c in anomaly_clues
        ]
    # 语义层统计（对象/链接行数）
    report["ontology"] = ontology_stats
    (out_dir / "lineage_clues.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # 合并 person/org 映射供操作台展示
    (out_dir / "entity_mapping.json").write_text(json.dumps(
        {"person": final_person_mapping, "org": accepted, "review": queue.summary()},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  ✅ {out_dir / 'lineage_clues.json'}")
    print(f"  ✅ {out_dir / 'entity_mapping.json'}（person={len(final_person_mapping)} org={len(accepted)}）")
    print(f"  ✅ {out_dir / 'review_queue.json'}（{queue.summary()}）")

    store.close()
    print("\n🎯 全管线执行完成。侦查逻辑一行未改，技术底座：DuckDB + LadybugDB 单机栈。")


# ----------------------------------------------------------------------
# 辅助：构造模拟多格式样本 + 庙算假设
# ----------------------------------------------------------------------
def _prepare_sample_files(sample_dir: Path) -> list[Path]:
    """生成 5 种格式的样本文件，演示适配层（真实场景替换为用户上传文件即可）。"""
    files: list[Path] = []
    # CSV（金额带逗号 + 分号分隔，测试自动检测）
    f = sample_dir / "银行流水_2024Q1.csv"
    f.write_text("日期;主体;对方;金额\n2024-03-15;张卫国;现金存入;1,000,000\n2024-06-20;李志强;财政局;18,532\n",
                 encoding="utf-8")
    files.append(f)
    # JSON（嵌套结构）
    f = sample_dir / "通话记录.json"
    f.write_text(json.dumps({"data": {"records": [
        {"caller": "张卫国", "callee": "李志强", "count": 12},
        {"caller": "张卫国", "callee": "王五", "count": 3},
    ]}}, ensure_ascii=False), encoding="utf-8")
    files.append(f)
    # SQLite（另一库，测试跨库读取）
    # 注1：必须用 sqlite3 建库——此前误用 duckdb.connect() 生成的是 DuckDB 文件，
    #      sqlite 适配器打开时报 "file is not a database"。
    # 注2：部分容器文件系统（如 overlay2）不支持 SQLite 的 journal/lock 机制，
    #      会抛 "disk I/O error" —— 此处容错跳过，不阻断其余 4 种格式的演示。
    import sqlite3
    f = sample_dir / "工商注册.sqlite"
    if f.exists():
        f.unlink()          # 清掉旧的错误格式文件（DuckDB 格式残留）
    try:
        con = sqlite3.connect(str(f))
        con.execute("CREATE TABLE IF NOT EXISTS companies (name VARCHAR, rep VARCHAR)")
        con.execute("DELETE FROM companies")
        con.execute("INSERT INTO companies VALUES ('宏业建设有限公司','李志强'),('A建材','李志强妻弟')")
        con.commit()
        con.close()
        files.append(f)
    except sqlite3.Error as e:
        print(f"  ⚠ 跳过 SQLite 样本（当前文件系统不支持：{e}）")
        f.unlink(missing_ok=True)
    # Parquet（原生直读，无需适配）
    import pandas as pd
    f = sample_dir / "中标公告.parquet"
    pd.DataFrame([{"项目": "滨江路改造", "公司": "宏业建设", "日期": "2024-01-10"}]).to_parquet(f)
    files.append(f)
    # Excel（含中文列名，需 openpyxl；不可用时跳过）
    try:
        import openpyxl  # noqa
        f = sample_dir / "工商信息.xlsx"
        pd.DataFrame([{"主体": "张卫国配偶", "关联": "张卫国"}]).to_excel(f, index=False)
        files.append(f)
    except ImportError:
        pass
    return files


def _run_graph_overpass(store) -> dict | None:
    """
    L4 图库层：Q2 过桥的 Cypher 多跳 + SQL 自连接双轨比对。

    数据入口：DuckDB → CSV → COPY 进 LadybugDB（ATTACH 需联网下载扩展，受限环境走 CSV）。
    可选组件：ladybug 不可用时返回 None，由调用方跳过（不阻断主线）。
    """
    from core.graph import GraphBackend, overpass_two_hop_sql, compare_engines

    g = GraphBackend("data/ladybug/investigation.lbug")
    if not g.available:
        print("  ⚠ 未安装 ladybug，图库轨跳过（仅保留 SQL 轨）：pip install ladybug")
        return None

    try:
        stat = g.build_from_duckdb(store)
        print(f"  建图：节点 {stat['nodes']} 个，边 {stat['edges']} 条")
        cypher_paths = g.overpass_two_hop()
        sql_paths = overpass_two_hop_sql(store)
        cmp_res = compare_engines(cypher_paths, sql_paths)

        for p in cypher_paths:
            print(f"  过桥路径：{p.source} → {p.bridge} → {p.dest} "
                  f"({p.amount_in:,.0f} → {p.amount_out:,.0f})")
        print(f"  双轨比对：Cypher {cmp_res['cypher_count']} 条 / SQL {cmp_res['sql_count']} 条 "
              f"→ {'✅ 一致' if cmp_res['consistent'] else '⚠ 不一致，须正兵复核'}")
        if not cmp_res["consistent"]:
            print(f"    仅 Cypher：{cmp_res['only_in_cypher']}")
            print(f"    仅 SQL：{cmp_res['only_in_sql']}")

        # 奇兵拓线：核心主体 2 跳邻域
        for subj in ["宏业建设", "张卫国"]:
            try:
                nb = g.neighbors_within(subj, max_hops=2)
                print(f"  {subj} 2跳邻域：{nb}")
            except Exception:
                pass

        cmp_res["paths"] = [p.to_dict() for p in cypher_paths]
        return cmp_res
    except Exception as e:
        print(f"  ⚠ 图库轨执行失败，降级为 SQL 单轨：{e}")
        return None
    finally:
        g.close()


def _build_miaosuan(store, ctx, health=None):
    """三层机制构建庙算：数据驱动 + 规则约束 + 人机协同。"""
    from core import MiaoSuan, Hypothesis
    miao = MiaoSuan()
    miao.set_ji(
        gaps=ctx["证据缺口"],           # 证据缺口（强制非空）
        auth_boundary=ctx["授权边界"],  # 授权边界（强制非空）
    )
    # 第 1 层 数据驱动：先跑虚实扫描，异常模式自动映射为候选假设（不再硬编码）
    from skills.xu_shi import run as xu_shi_run
    findings = xu_shi_run(ctx, store, health=health)["虚实扫描"]["findings"]
    added = miao.auto_from_findings(findings)
    print(f"  数据驱动：异常扫描 {len(findings)} 项 → 自动生成假设 "
          f"{[h.id for h in added]}")
    # 第 3 层 人机协同：正兵手动补充 H5（受限演示：房产车辆未调取）
    miao.add(Hypothesis(
        id="H5",
        description="张卫国隐匿财产",
        evidence_needed=["房产", "车辆"],
        data_sources=["房产车辆（未调取）"],
        procedure="待批",
        falsification="资产与合法收入匹配则证伪",
        dimension=["行为"],
        jian_types=["内间"],
    ))
    # 第 2 层 规则约束：受限/降级标记（build 内置）
    miao.build(ctx["可用数据"], ctx["未调取"])
    # 第 3 层 枚举空间：笛卡尔积候选池 + 候补清单
    miao.enumerate_space()
    # 覆盖完整性量化指标（维度/数据源/间类/冲突）
    cov = miao.report(ctx["可用数据"])
    dc = cov["dimension_coverage"]
    print(f"  覆盖度：维度 {len(dc['covered'])}/5（{dc['score']:.0%}）"
          f" 数据源 {cov['data_source_coverage']['score']}%"
          f" → {'⚠ ' + dc['alarm_text'] if dc['alarm'] else '无报警'}")
    # REQ-G-024：实证缺口独立于声明报警，控制台同步可见（否则声明满覆盖时
    # 上面打印"无报警"，健康度却因实证缺口 degraded，自相矛盾）
    if dc.get("empirical_alarm"):
        print(f"  实证覆盖：⚠ {dc['empirical_alarm_text']}")
    print(f"  间类缺口：{cov['jian_coverage']['missing'] or '无'}；"
          f"证据冲突：{len(cov['conflicts'])} 处；"
          f"枚举候选池 {cov['enum']['total_combos']} 组合 → 候补 {cov['enum']['backlog_size']}")
    return miao


if __name__ == "__main__":
    main()
