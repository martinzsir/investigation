#!/usr/bin/env python3
"""确定性采集：产出研判报告的「第二段/第五段」事实块（已脱敏）。

设计要点：
  1. 纯确定性——不调用任何 LLM，数字来自内核计算或声明文件；
  2. per-case——只消费指定案件的数据根，绝不读全局产物；
  3. 脱敏前置——输出即脱敏，外部 Agent 拿到的已是安全载荷；
  4. 降级不静默——取不到的项写进 "降级" 数组，报告必须如实标注。

用法：
    python gather_evidence.py --case C001 --clue L3 --out evidence.json
    python gather_evidence.py --case C001 --demo
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# 项目根定位
# ----------------------------------------------------------------------


def find_project_root(explicit: str | None = None) -> Path:
    """定位侦查内核项目根（含 core/ 与 ontology/ 的目录）。"""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not (p / "core").exists():
            raise SystemExit(f"--project-root 不像项目根（缺 core/）：{p}")
        return p
    env = Path.cwd()
    for base in (env, *env.parents):
        if (base / "core").exists() and (base / "ontology").exists():
            return base
    raise SystemExit(
        "未找到项目根（未发现同时含 core/ 与 ontology/ 的目录）。\n"
        "请用 --project-root 指定，或设置环境变量 SUNZI_ROOT。"
    )


# ----------------------------------------------------------------------
# 脱敏
# ----------------------------------------------------------------------

# 按 llm_policy.json 的 pii_redaction：id_card/phone/bank_card = redact
#
# 注意：不能用 \b 作边界——Python re 默认 Unicode 模式下中文属 \w，
# "身份证110101199003071234" 中「证」与「1」之间 \b 不成立，会漏脱敏。
# 统一改用 (?<!\d) / (?!\d) 数字边界（实测修复）。
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证已遮蔽]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号已遮蔽]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[银行卡号已遮蔽]"),
]


def redact(obj: Any) -> Any:
    """递归脱敏：对字符串应用 PII 正则，容器递归。"""
    if isinstance(obj, str):
        out = obj
        for pat, repl in _PII_PATTERNS:
            out = pat.sub(repl, out)
        return out
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# ----------------------------------------------------------------------
# 采集
# ----------------------------------------------------------------------


def load_jians(root: Path, pack: str) -> dict[str, Any]:
    """读五间声明（P6：词汇唯一权威在 packs/wujian/jians.json）。"""
    f = root / "packs" / "wujian" / "jians.json"
    if not f.exists():  # 兼容旧布局快照
        f = root / "ontology" / pack / "jians.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_rules(root: Path, pack: str) -> list[dict[str, Any]]:
    """读规则手册全集（确定性，供第五段引用）。"""
    f = root / "ontology" / pack / "rules.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("rules", data) if isinstance(data, dict) else data
    out = []
    for r in items if isinstance(items, list) else []:
        out.append({
            "id": r.get("id", ""),
            "名称": r.get("name", r.get("title", "")),
            "判据": r.get("condition", r.get("rule_text", "")),
            "间类": r.get("jian_types", r.get("jian", [])),
        })
    return out


def collect_cross(root: Path, pack: str, degraded: list[str]) -> dict[str, Any]:
    """五间覆盖 + 交叉等级 + 覆盖缺口（确定性）。"""
    jians = load_jians(root, pack)
    declared = []
    for item in jians.get("jians", []) if isinstance(jians, dict) else []:
        declared.append({
            "名称": item.get("name", ""),
            "权重": item.get("weight"),
            "密级": item.get("default_clearance"),
            "数据源": item.get("source_object_types", []),
        })

    hit: dict[str, int] = {}
    level = None
    cross_ok = True
    try:
        sys.path.insert(0, str(root))
        from core import Store  # type: ignore
        from core.functions import invoke_function  # type: ignore

        store = Store(root=str(root / "data"))
        r = invoke_function(store, "jian_cross_level")["result"]
        hit = {k: len(v) for k, v in (r.get("rows") or {}).items()} \
            if isinstance(r.get("rows"), dict) else {}
        for row in (r.get("rows") or []) if isinstance(r.get("rows"), list) else []:
            hit[row.get("间类", "")] = row.get("命中数", 0)
        level = r.get("交叉等级")
    except Exception as exc:  # 内核不可用时降级，不静默
        cross_ok = False
        degraded.append(f"五间交叉不可用（{type(exc).__name__}）：未 BUILD 或内核未就绪")

    covered = {k for k, v in hit.items() if v}

    # 降级时不得把"算不出来"呈现为"全部未覆盖"——后者会被误读为真实结论。
    if not cross_ok:
        missing: list[str] | None = None
    else:
        missing = sorted({d["名称"] for d in declared} - covered) if declared else []

    return {
        "五间声明": declared,
        "命中间类": sorted(covered),
        "命中计数": hit,
        "交叉等级": level,
        "覆盖缺口": missing,
        "覆盖缺口已知": cross_ok,
        "等级规则": "单源=观察 / 双源=线索 / 三源以上=可立案依据候选",
    }


def collect_overpass(root: Path, degraded: list[str]) -> list[dict[str, Any]]:
    """图库两跳过桥路径（不可用时降级，绝不伪造路径）。"""
    try:
        sys.path.insert(0, str(root))
        from core import Store  # type: ignore
        from core.graph import GraphBackend, overpass_two_hop_sql  # type: ignore

        store = Store(root=str(root / "data"))
        paths = overpass_two_hop_sql(store) or []
        # OverpassPath 是 dataclass，json.dumps 不能直接序列化，转 dict
        return [p.to_dict() if hasattr(p, "to_dict") else vars(p) for p in paths]
    except Exception as exc:
        degraded.append(f"过桥路径不可用（{type(exc).__name__}）：图库未构建或非必需")
        return []


def load_schema_version(root: Path, pack: str) -> int | None:
    """读本体包 schema_version（objects.json 顶层；仅用于报告展示，失败不降级）。"""
    f = root / "ontology" / pack / "objects.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        v = data.get("schema_version")
        return int(v) if v is not None else None
    except Exception:
        return None


def _collect_registered_sources(root: Path, case_id: str,
                                degraded: list[str]) -> tuple[list[dict[str, Any]], int]:
    """读 meta/meta.db 的 case_sources 登记（按 case_id 严格过滤）。

    只把 status=imported 作为报告依据批次；staged 等未导入批次只给计数，
    避免把"未进入语义层"的上传误呈现为证据来源。库不可读 → 降级。
    """
    meta_db = root / "meta" / "meta.db"
    if not meta_db.exists():
        degraded.append(f"数据源登记库不存在：{meta_db}（meta 未初始化）")
        return [], 0
    try:
        conn = sqlite3.connect(str(meta_db))
        try:
            rows = conn.execute(
                "SELECT upload_id, filename, fmt, table_name, rows, status, "
                "created_at FROM case_sources WHERE case_id=? "
                "ORDER BY created_at, upload_id",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        degraded.append(
            f"数据源登记不可读（{type(exc).__name__}）：meta/meta.db 的 "
            "case_sources 未能查询")
        return [], 0

    imported: list[dict[str, Any]] = []
    staged = 0
    for upload_id, filename, fmt, table_name, n_rows, status, created_at in rows:
        if status == "imported":
            imported.append({
                "upload_id": upload_id,
                "filename": filename,
                "fmt": fmt,
                "table_name": table_name or "",
                "rows": n_rows,
                "created_at": created_at,
            })
        else:
            staged += 1
    return imported, staged


def _collect_clue_sources(root: Path, case_id: str,
                          clue_id: str) -> list[dict[str, Any]]:
    """聚合本线索画布引用的数据源（source_row/source_file 节点）。

    数据源以 source_row 节点计数（= 该线索溯源行数），文件名/上传批次
    取同 dataset 的 source_file 节点 props。
    无 state.sqlite（无画布功能的案件）→ 返回空列表，不降级；
    库在但查询/解析失败 → 抛异常，由调用方记降级（不静默）。
    """
    if not clue_id:
        return []
    state_db = root / "cases" / case_id / "state.sqlite"
    if not state_db.exists():
        return []

    conn = sqlite3.connect(str(state_db))
    try:
        row = conn.execute(
            "SELECT doc_json FROM clue_canvas WHERE clue_id=? "
            "ORDER BY version DESC LIMIT 1",
            (clue_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []

    doc = json.loads(row[0])
    file_nodes: dict[str, dict[str, str]] = {}
    counts: collections.Counter[str] = collections.Counter()
    for n in doc.get("nodes", []) or []:
        kind = n.get("kind")
        props = n.get("props") or {}
        if kind == "source_file" and props.get("registered"):
            ds = str(props.get("dataset") or "")
            if ds:
                file_nodes[ds] = {
                    "upload_id": str(props.get("upload_id") or ""),
                    "filename": str(n.get("label") or ""),
                }
        elif kind == "source_row":
            ds = str(props.get("source") or "")
            if not ds:
                ref = str(n.get("ref") or "")
                ds = ref.split("@", 1)[0] if "@" in ref else ""
            if ds:
                counts[ds] += 1

    out: list[dict[str, Any]] = []
    for ds, n_rows in sorted(counts.items()):
        finfo = file_nodes.get(ds, {})
        out.append({
            "dataset": ds,
            "filename": finfo.get("filename", ""),
            "upload_id": finfo.get("upload_id", ""),
            "溯源行数": n_rows,
        })
    return out


def _split_obj_ref(ref: str, prefix: str) -> tuple[str, str]:
    """obj_person#p1 / lnk_transfers#pk → (名, 键)；形态不符返回 ('','')。"""
    if isinstance(ref, str) and ref.startswith(prefix) and "#" in ref:
        name, key = ref[len(prefix):].split("#", 1)
        return name, key
    return "", ""


def collect_clue_refs(root: Path, case_id: str, clue_id: str,
                      degraded: list[str]) -> dict[str, Any] | None:
    """P7 线索证据引用确定性块：从最新线索产物按 evidence_refs 聚合。

    任何新研判镜头只要按契约产出 evidence_refs，其节点/边/时间窗/
    聚合量/书证即在此自动出现（新镜头零接线进报告）。
      - clue_id 非空时仅取该线索；空=案件全部线索；
      - 无任何线索产物 → None（区别于"有产物但无引用"）；
      - 产物存在但解析失败 → 记降级，返回空组块；
      - P8 回流并集：state.sqlite image_evidence（人验通过）按线索并入
        书证引用——图像核验发生在线索产物产出之后（证据回流），镜头
        无法预挂 file 引用，由报告侧确定性采集（file_uri 去重，产物
        条目优先），非 LLM 转述。
    """
    art_dir = root / "cases" / case_id / "artifacts"
    try:
        files = [p for p in art_dir.glob("clues_v*.json")
                 if p.stem[len("clues_v"):].isdigit()]
    except Exception:
        files = []
    if not files:
        return None
    latest_p = max(files, key=lambda p: int(p.stem[len("clues_v"):]))
    try:
        data = json.loads(latest_p.read_text(encoding="utf-8"))
        raws = data.get("clues", []) if isinstance(data, dict) else []
    except Exception as exc:
        degraded.append(
            f"线索证据引用不可读（{type(exc).__name__}）：{latest_p.name} "
            "未能解析，第五段不含图谱/书证引用")
        raws = []

    block: dict[str, Any] = {
        "artifact_version": int(latest_p.stem[len("clues_v"):]),
        "线索数": 0,
        "节点引用": [],
        "边引用": [],
        "时间窗": [],
        "聚合量": [],
        "书证引用": [],
    }
    titles: dict[str, str] = {}
    for r in raws if isinstance(raws, list) else []:
        if not isinstance(r, dict):
            continue
        if clue_id and r.get("clue_id") != clue_id:
            continue
        cid = str(r.get("clue_id") or "")
        title = str(r.get("title") or "")
        titles[cid] = title
        block["线索数"] += 1
        for ref in r.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            kind = ref.get("kind")
            raw_ref = str(ref.get("ref") or "")
            base = {"线索": cid, "线索标题": title}
            if kind == "node":
                tname, key = _split_obj_ref(raw_ref, "obj_")
                if tname and key:
                    block["节点引用"].append(
                        {**base, "type": tname, "key": key})
            elif kind == "edge":
                tname, pk = _split_obj_ref(raw_ref, "lnk_")
                if tname and pk:
                    block["边引用"].append(
                        {**base, "type": tname, "edge_pk": pk})
            elif kind == "time_window":
                tname, pk = _split_obj_ref(raw_ref, "lnk_")
                block["时间窗"].append({
                    **base, "type": tname, "edge_pk": pk,
                    "time_from": ref.get("time_from"),
                    "time_to": ref.get("time_to")})
            elif kind == "aggregate":
                block["聚合量"].append({
                    **base,
                    "metric": str(ref.get("metric") or ""),
                    "value": ref.get("value")})
            elif kind == "file":
                block["书证引用"].append({
                    **base,
                    "file_uri": str(ref.get("file_uri")
                                   or ref.get("uri") or raw_ref),
                    "verify_status": ref.get("verify_status")})
    # P8 回流并集：人验通过的图像证据按线索并入书证引用（file_uri 去重，
    # 产物条目优先——镜头已挂的同_uri 书证保留镜头给的核验状态）
    try:
        images = collect_image_evidence(root, case_id, clue_id)
    except Exception as exc:
        degraded.append(
            f"图像书证引用不可读（{type(exc).__name__}）：state.sqlite 的 "
            "image_evidence 未能并入书证引用")
        images = []
    seen_uris = {b["file_uri"] for b in block["书证引用"]}
    for im in images:
        uri = str(im.get("image_uri") or "")
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        im_clue = str(im.get("clue_id") or "")
        block["书证引用"].append({
            "线索": im_clue,
            "线索标题": titles.get(im_clue, ""),
            "file_uri": uri,
            "verify_status": im.get("verify_conclusion") or "",
            "verifier": im.get("verifier") or "",
        })
    return block


def collect_sources(root: Path, case_id: str, clue_id: str, pack: str,
                    version: int | None,
                    degraded: list[str]) -> dict[str, Any]:
    """第九段确定性块：数据文件/批次/行数/版本 + 本线索引用数据源。

    纯登记口径（ingest meta + 画布节点），不调用 LLM、不读明细值。
    """
    registered, staged_count = _collect_registered_sources(
        root, case_id, degraded)

    clue_sources: list[dict[str, Any]] = []
    if clue_id:
        try:
            clue_sources = _collect_clue_sources(root, case_id, clue_id)
        except Exception as exc:
            degraded.append(
                f"本线索数据源聚合不可用（{type(exc).__name__}）：画布文档"
                "未能解析，第九段仅展示案件级登记")

    return {
        "案件库": (f"cases/{case_id}/v{version}.duckdb"
                  if version is not None else None),
        "数据版本": version,
        "本体包": f"ontology/{pack}",
        "本体版本": load_schema_version(root, pack),
        "登记数据源": registered,
        "未导入批次计数": staged_count,
        "本线索数据源": clue_sources,
    }


def collect_image_evidence(root: Path, case_id: str,
                           clue_id: str) -> list[dict[str, Any]]:
    """P8 人验通过的图像证据（state.sqlite image_evidence 表）。

    核验（人比对原件）后写入，此处自动采集——证据核验后自动入报告。
      - 无 state.sqlite → []（不降级，与画布同口径）；
      - 库在但查询失败 → 抛异常，由调用方记降级（不静默）；
      - clue_id 非空仅取该线索；
      - 列取白名单与实际列交集（旧库缺 title/detail/severity 不降级，
        缺基础列才返回 []——理论不可达，防御性兜底）。
    """
    state_db = root / "cases" / case_id / "state.sqlite"
    if not state_db.exists():
        return []
    conn = sqlite3.connect(str(state_db))
    try:
        available = {r[1] for r in conn.execute(
            "PRAGMA table_info(image_evidence)").fetchall()}
        want = ["image_evidence_id", "clue_id", "image_uri", "model",
                "prompt_version", "model_score", "title", "detail",
                "severity", "verifier", "verify_conclusion",
                "subject_type", "subject_id", "draft_id", "created_at"]
        sel = [c for c in want if c in available]
        if not sel:
            return []
        sql = f"SELECT {', '.join(sel)} FROM image_evidence WHERE case_id=?"
        args: list[Any] = [case_id]
        if clue_id:
            sql += " AND clue_id=?"
            args.append(clue_id)
        rows = conn.execute(
            sql + " ORDER BY created_at DESC, rowid DESC", args).fetchall()
    finally:
        conn.close()
    return [dict(zip(sel, r)) for r in rows]


def collect_image_evidence_safe(root: Path, case_id: str, clue_id: str,
                                degraded: list[str]) -> list[dict[str, Any]]:
    """collect_image_evidence 的降级包装：查询失败记降级返回 []（不静默）。

    CLI main() 与 MCP report.gather_evidence 共用，降级措辞单一出处。
    """
    try:
        return collect_image_evidence(root, case_id, clue_id)
    except Exception as exc:
        degraded.append(
            f"图像证据不可读（{type(exc).__name__}）：state.sqlite 的 "
            "image_evidence 未能查询")
        return []


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="研判报告确定性采集（已脱敏）")
    ap.add_argument("--case", required=True, help="案件 ID")
    ap.add_argument("--clue", default="", help="线索 ID（可空=全案件）")
    ap.add_argument("--pack", default="default", help="本体包")
    ap.add_argument("--out", default="", help="输出 JSON 路径（默认 stdout）")
    ap.add_argument("--project-root", default="", help="侦查内核项目根")
    ap.add_argument("--demo", action="store_true", help="降级演示（无案件库也可跑）")
    args = ap.parse_args()

    root = find_project_root(args.project_root or None)
    degraded: list[str] = []

    # 案件数据版本（读不到则降级，不冒充 0）
    version: int | None = None
    case_duckdb = root / "cases" / args.case
    if case_duckdb.exists():
        vs = sorted(
            (int(p.stem[1:]) for p in case_duckdb.glob("v*.duckdb") if p.stem[1:].isdigit()),
            reverse=True,
        )
        version = vs[0] if vs else None
    else:
        degraded.append(f"案件库不存在：{case_duckdb}（未 BUILD 或 case_id 有误）")

    if version is None and not args.demo:
        degraded.append("未解析到 data_version：报告将缺少数据版本锚点")

    # P8 人验通过的图像证据（state.sqlite；无库=空，查询失败记降级）
    image_evidence = collect_image_evidence_safe(
        root, args.case, args.clue, degraded)

    evidence = {
        "case_id": args.case,
        "clue_id": args.clue,
        "pack": args.pack,
        "data_version": version,
        "确定性块": {
            "证据充分性": collect_cross(root, args.pack, degraded),
            "关联核验": {
                "过桥路径": collect_overpass(root, degraded),
                "规则手册": load_rules(root, args.pack),
            },
            "线索证据引用": collect_clue_refs(
                root, args.case, args.clue, degraded),
            "图像证据": image_evidence,
            "数据源清单": collect_sources(
                root, args.case, args.clue, args.pack, version, degraded),
        },
        "降级": degraded,
        "脱敏": True,
        "生成方式": "deterministic（无 LLM 参与）",
    }

    safe = redact(evidence)
    text = json.dumps(safe, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}（降级项 {len(degraded)} 条）", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
