"""
core/data_map.py
REQ-P M 波（025~030）：数据地图 —— L0 静态拓扑 + L1 物理血缘。

零依赖红线（REQ-P-030，AC 固化）：本模块只 import json/re，不 import duckdb、
不 import core 其他模块——L0/L1 全部来自对 ontology/<pack>/{objects,links,bindings}.json
的静态解析，不连接数据库。数据地图只观察不写回；全部结论均为【待核实】候选，
不构成办案指导。

四个已修缺陷的判据内置（实现 + tests/test_data_map.py 双层固化）：
  缺陷1  物理度累计发生在全部 link build_sql 解析完成之后、孤儿判定之前——隐形枢纽
         （语义度 0 却物理支撑边的对象，如 call/trackpoint）不会被误判成孤立；
  缺陷2  等值归一判定 = JOIN 条件两侧属性名都含 "raw"——p.raw_name = a.raw_name
         （owns/osint_mentions）正确判归一，不因两侧同名被误判为业务条件；
  缺陷3  归一定向 = JOIN 的表是 target、另一侧是 source（不按等号左右定向）；
  缺陷4  归一缺口判据看 build_sql 是否已等值归一，不看 links.json 端点、
         不看属性名是否在 SQL 中出现。

已知行为固化（REQ-P-029）：TABLE_ALIAS_RE 会把 SQL 关键字 JOIN 抓成别名
（FROM obj_transaction JOIN ... 中 obj_transaction 的"别名"被误抓为 JOIN）；
过滤在 _alias_map 层做、不在正则层——test_ac16 固化该行为，防后人"修正则"
破坏已验证行为。
"""
from __future__ import annotations

import json
import re

# L0 判定阈值：语义度 ≥ 该值判"核心枢纽"（default 包 person=8）
SEMANTIC_CORE_THRESHOLD = 5

# 表引用：FROM/JOIN 后跟 obj_*（前提：声明 SQL 无 CTE/动态表名——test_ac20 固化该前提，
# 将来引入 CTE 时该测试失败提醒重估）
TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+(obj_\w+)", re.IGNORECASE)
# 别名：obj_* 后可选跟 [AS] 别名。已知行为：会把 SQL 关键字（如 JOIN）误抓为别名，
# 过滤在 _alias_map 层做、不在正则层（test_ac16 固化）。
TABLE_ALIAS_RE = re.compile(r"\b(obj_\w+)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.IGNORECASE)
# 等值条件：alias.col = alias.col（<> / <= 不误中——time_window 的 ABS(...)<=20 不算归一）
EQUI_RE = re.compile(r"(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)")
# 对象 source_sql 的源表（L2/L3 业务表，如 通话记录）
FROM_TABLE_RE = re.compile(r"\bFROM\s+([^\s(),;]+)", re.IGNORECASE)
JOIN_SPLIT_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)

# SQL 关键字黑名单：TABLE_ALIAS_RE 误抓的"别名"在此层过滤
SQL_KEYWORDS = {
    "join", "inner", "left", "right", "full", "outer", "cross", "on", "where",
    "and", "or", "as", "select", "from", "group", "order", "by", "union", "all",
    "case", "when", "then", "else", "end", "cast", "not", "in", "is", "null",
    "limit", "having", "distinct",
}


def _alias_map(sql: str) -> dict[str, str]:
    """alias → obj 表名。TABLE_ALIAS_RE 的关键字误抓在此层过滤（不在正则层，
    见模块 docstring 已知行为固化）。"""
    out: dict[str, str] = {}
    for table, alias in TABLE_ALIAS_RE.findall(sql):
        if not alias:
            continue
        a = alias.lower()
        if a in SQL_KEYWORDS:
            continue
        out.setdefault(a, table.lower())
    return out


def _parse_link_sql(sql: str, binding: dict) -> dict:
    """解析 link build_sql：主表、全部 obj 表、别名映射、JOIN 逐段、归一对。"""
    tables = [t.lower() for t in TABLE_RE.findall(sql)]
    amap = _alias_map(sql)
    tset = set(tables)
    joins = []
    chunks = JOIN_SPLIT_RE.split(sql)
    for ch in chunks[1:]:
        tm = re.search(r"\b(obj_\w+)", ch, re.IGNORECASE)
        if not tm:
            continue
        table = tm.group(1).lower()
        am = re.search(r"\bobj_\w+\s+(?:AS\s+)?([A-Za-z_]\w*)", ch, re.IGNORECASE)
        alias = None
        if am and am.group(1).lower() not in SQL_KEYWORDS:
            alias = am.group(1)
        parts = re.split(r"\bON\b", ch, maxsplit=1, flags=re.IGNORECASE)
        on = parts[1].strip() if len(parts) > 1 else ""
        joins.append({"table": table, "alias": alias, "on": on})

    def _resolve_obj(tok: str) -> str | None:
        t = tok.lower()
        table = amap.get(t) or (t if t in tset else None)
        if table is None:
            return None
        return table[len("obj_"):] if table.startswith("obj_") else table

    declared = {(str(d.get("table", "")).lower(), str(d.get("alias", "")).lower())
                for d in (binding.get("normalize") or [])}
    normalizes = []
    jtable_obj: str | None = None
    for j in joins:
        jtable_obj = j["table"][len("obj_"):] if j["table"].startswith("obj_") else j["table"]
        jalias = (j["alias"] or "").lower()
        for a1, c1, a2, c2 in EQUI_RE.findall(j["on"]):
            # 缺陷 2：两侧属性名都含 raw 才算等值归一（p.raw_name = a.raw_name 不误判）
            if "raw" not in c1.lower() or "raw" not in c2.lower():
                continue
            o1, o2 = _resolve_obj(a1), _resolve_obj(a2)
            if o1 is None or o2 is None:
                continue
            # 缺陷 3：JOIN 的表是 target，另一侧是 source（不按等号左右定向）
            if a1.lower() == jalias or o1 == jtable_obj:
                target, source = (o1, c1), (o2, c2)
            elif a2.lower() == jalias or o2 == jtable_obj:
                target, source = (o2, c2), (o1, c1)
            else:
                continue
            normalizes.append({
                "target_obj": target[0], "target_prop": target[1],
                "source_obj": source[0], "source_prop": source[1],
                "on": f"{a1}.{c1} = {a2}.{c2}",
                "join_table": j["table"], "join_alias": j["alias"],
                "declared": (j["table"], jalias) in declared,
            })
    main = tables[0] if tables else None
    return {
        "sql": sql,
        "main": main,
        "tables": tables,
        "aliases": amap,
        "joins": joins,
        "normalizes": normalizes,
        # REQ-P-029 前提标记：出现 WITH 即 CTE——TABLE_RE 提取策略需重估（测试失败提醒）
        "has_with": bool(re.search(r"\bWITH\b", sql, re.IGNORECASE)),
    }


class DataMap:
    """数据地图 L0（静态拓扑）+ L1（物理血缘）。

    全部输入为声明 JSON 的原始 dict（零依赖：不连库、不 import core 其他模块）。
    用法：DataMap.from_pack("ontology", "default")。
    """

    def __init__(self, pack: str, objects: list[dict], links: list[dict],
                 bindings: dict | None):
        self.pack = pack
        self.objects = {o["name"]: o for o in (objects or [])}
        self.links = {l["name"]: l for l in (links or [])}
        self.bindings = bindings
        self.notes: list[str] = []
        self._ob: list[dict] = (bindings or {}).get("object_bindings") or []
        self._lb: list[dict] = (bindings or {}).get("link_bindings") or []
        if bindings is None:
            self.notes.append(
                "bindings.json 缺失——归一缺口未计算（不是「无缺口」）；对象/边血缘不可用")
        # ---- 缺陷 1 顺序锁：先解析全部 link build_sql（物理度在此累计），
        # 后做孤儿/判定——隐形枢纽（call/trackpoint）不丢 ----
        self._parsed: dict[str, dict] = {
            lb.get("link", ""): _parse_link_sql(lb.get("build_sql") or "", lb)
            for lb in self._lb
        }
        self._physical: dict[str, int] = {}
        for p in self._parsed.values():
            for t in set(p["tables"]):
                obj = t[len("obj_"):] if t.startswith("obj_") else t
                self._physical[obj] = self._physical.get(obj, 0) + 1
        self._semantic: dict[str, int] = {}
        for l in self.links.values():
            if l.get("runtime"):
                continue
            for k in ("from_obj", "to_obj"):
                o = l.get(k)
                if o:
                    self._semantic[o] = self._semantic.get(o, 0) + 1

    # ----------------------------------------------------------------------
    # 装载
    # ----------------------------------------------------------------------
    @classmethod
    def from_pack(cls, ontology_dir, pack: str = "default") -> "DataMap":
        """读 ontology/<pack>/{objects,links,bindings}.json（bindings 可缺失）。"""
        base = str(ontology_dir).rstrip("/\\") + "/" + pack

        def _read(name: str):
            try:
                with open(base + "/" + name, encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return None

        obj_doc = _read("objects.json") or {}
        link_doc = _read("links.json") or {}
        return cls(pack=pack, objects=obj_doc.get("objects") or [],
                   links=link_doc.get("links") or [],
                   bindings=_read("bindings.json"))

    # ----------------------------------------------------------------------
    # L0 静态拓扑（REQ-P-025）
    # ----------------------------------------------------------------------
    def _raw_candidates(self) -> list[tuple[str, str]]:
        """需要归一判定的 raw 引用属性（对象.属性）。

        排除：entity 对象的 name_property（归一目标本身的身份列）、
        metadata_props（REQ-P-034 内容字段排除，如 tipoff.content_raw）、runtime 对象。
        """
        out = []
        for name, o in self.objects.items():
            if o.get("runtime"):
                continue
            nprop = o.get("name_property")
            meta = set(o.get("metadata_props") or [])
            for prop in (o.get("properties") or {}):
                if "raw" not in prop.lower():
                    continue
                if o.get("kind") == "entity" and prop == nprop:
                    continue
                if prop in meta:
                    continue
                out.append((name, prop))
        return out

    def objects_inventory(self) -> list[dict]:
        """L0 对象资产清单：语义度/物理度/判定（核心枢纽|枢纽|★ 隐形枢纽|孤立）。"""
        cands: dict[str, list[str]] = {}
        for obj, prop in self._raw_candidates():
            cands.setdefault(obj, []).append(prop)
        inv = []
        for name, o in self.objects.items():
            sem = self._semantic.get(name, 0)
            phy = self._physical.get(name, 0)
            hidden = sem == 0 and phy > 0
            orphan = sem == 0 and phy == 0
            if hidden:
                verdict = "★ 隐形枢纽"
            elif orphan:
                verdict = "孤立" + ("（runtime）" if o.get("runtime") else "")
            elif sem >= SEMANTIC_CORE_THRESHOLD:
                verdict = "核心枢纽"
            else:
                verdict = "枢纽"
            inv.append({
                "name": name,
                "title": o.get("title", ""),
                "kind": o.get("kind", ""),
                "runtime": bool(o.get("runtime")),
                "semantic_degree": sem,     # 非 runtime 链接 from_obj/to_obj 出现次数
                "physical_degree": phy,     # 引用该对象的链接绑定数（每链接计 1）
                "hidden_hub": hidden,
                "orphan": orphan,
                "verdict": verdict,
                "raw_props": cands.get(name, []),   # 待归一属性候选（已排除身份列/元数据列）
                "metadata_props": list(o.get("metadata_props") or []),
            })
        return inv

    # ----------------------------------------------------------------------
    # L1 物理血缘（REQ-P-026）
    # ----------------------------------------------------------------------
    def lineage(self) -> dict:
        """对象←源表（UNION 拆解）、边←物理来源对象、清洗规则清单。"""
        objs: dict[str, dict] = {}
        for ob in self._ob:
            name = ob.get("object", "")
            if ob.get("source_sql"):
                tabs = FROM_TABLE_RE.findall(ob["source_sql"])
            elif isinstance(ob.get("source"), dict) and ob["source"].get("table"):
                tabs = [ob["source"]["table"]]
            else:
                tabs = []
            objs[name] = {
                "source_table": ob.get("source_table") or (tabs[0] if tabs else ""),
                "source_tables": sorted(set(tabs)),
                "union_branches": len(tabs),
                "clean": list(ob.get("clean") or []),
                "optional": bool(ob.get("optional")),
            }
        links_out: dict[str, dict] = {}
        for lname, p in self._parsed.items():
            src_objs = sorted({t[len("obj_"):] if t.startswith("obj_") else t
                               for t in set(p["tables"])})
            declared = self.links.get(lname, {})
            links_out[lname] = {
                "declared": [declared.get("from_obj", ""), declared.get("to_obj", "")],
                "source_objects": src_objs,
                "kind": "归一连接" if p["normalizes"] else "业务条件连接",
            }
        clean_rules: list[str] = []
        for info in objs.values():
            for c in info["clean"]:
                if c not in clean_rules:
                    clean_rules.append(c)
        return {"objects": objs, "links": links_out,
                "clean_rules": clean_rules, "notes": list(self.notes)}

    # ----------------------------------------------------------------------
    # 归一 JOIN 清单 / 定向与判定（REQ-P-027）
    # ----------------------------------------------------------------------
    def normalize_joins(self) -> list[dict]:
        """全部等值归一 JOIN：source_obj.source_prop → target_obj.target_prop。

        equal_raw 恒 True（两侧属性名都含 raw 才入选）；declared = 是否有
        bindings.json normalize 段声明（REQ-P-033）。
        """
        out = []
        for lname, p in self._parsed.items():
            for n in p["normalizes"]:
                out.append({"link": lname, **n, "equal_raw": True})
        return out

    # ----------------------------------------------------------------------
    # 归一缺口（REQ-P-028，缺陷 4：判据看 build_sql）
    # ----------------------------------------------------------------------
    def normalize_gaps(self) -> list[dict] | None:
        """未被任何 build_sql 等值归一的 raw 引用属性。

        None = bindings 缺失，无法判定（notes 声明"缺口未计算"≠"无缺口"）。
        """
        if self.bindings is None:
            return None
        covered = {(n["source_obj"], n["source_prop"])
                   for p in self._parsed.values() for n in p["normalizes"]}
        return [{"object": obj, "prop": prop,
                 "note": "该 raw 引用未被任何 link build_sql 等值归一——断链温床【待核实】"}
                for (obj, prop) in self._raw_candidates() if (obj, prop) not in covered]

    def _broken_links(self) -> set[str]:
        """物化 SQL 直接产出未归一 raw 引用列的链接（渲染虚线用）。"""
        gaps = self.normalize_gaps() or []
        if not gaps:
            return set()
        gap_by_obj: dict[str, set[str]] = {}
        for g in gaps:
            gap_by_obj.setdefault(g["object"], set()).add(g["prop"])
        broken = set()
        for lname, p in self._parsed.items():
            main = p["main"]
            if not main:
                continue
            mo = main[len("obj_"):] if main.startswith("obj_") else main
            for prop in gap_by_obj.get(mo, ()):
                if re.search(rf"\b{re.escape(prop)}\b", p["sql"], re.IGNORECASE):
                    broken.add(lname)
                    break
        return broken

    # ----------------------------------------------------------------------
    # 渲染（REQ-P-030）
    # ----------------------------------------------------------------------
    def render_markdown(self) -> str:
        lines: list[str] = []
        ap = lines.append
        ap(f"# 数据地图 L0 + L1 —— 案件包「{self.pack}」")
        ap("")
        ap("> 红线：只观察不写回。全部结论均为【待核实】候选，不构成办案指导。")
        ap("> 生成方式：静态解析声明 JSON（L0/L1 零依赖，未连接数据库）。")
        ap("")
        ap("## L0 静态拓扑（REQ-P-025）")
        ap("")
        ap("| 对象 | 语义度 | 物理度 | 判定 |")
        ap("|---|---:|---:|---|")
        for it in self.objects_inventory():
            title = f"{it['name']}（{it['title']}）" if it["title"] else it["name"]
            ap(f"| {title} | {it['semantic_degree']} | {it['physical_degree']} "
               f"| {it['verdict']} |")
        ap("")
        ap("语义度 = 非 runtime 链接 from_obj/to_obj 出现次数；"
           "物理度 = 引用该对象的链接绑定数（每链接计 1）。")
        ap("")
        ap("## L1 物理血缘（REQ-P-026）")
        ap("")
        ap("### 对象 ← 源表")
        ap("")
        ap("| 对象 | 源表 | 分支数 | 清洗规则 | optional |")
        ap("|---|---|---:|---|---|")
        lin = self.lineage()
        for name, info in lin["objects"].items():
            ap(f"| {name} | {info['source_table'] or '—'} | {info['union_branches']} "
               f"| {('、'.join(info['clean'])) or '—'} | {'是' if info['optional'] else '—'} |")
        ap("")
        ap("### 边 ← 物理来源对象")
        ap("")
        ap("| 链接 | 声明端点 | 物理来源对象 | 连接类型 |")
        ap("|---|---|---|---|")
        for lname, info in lin["links"].items():
            decl = " → ".join(x for x in info["declared"] if x) or "—"
            ap(f"| {lname} | {decl} | {('、'.join(info['source_objects'])) or '—'} "
               f"| {info['kind']} |")
        ap("")
        ap(f"### 清洗规则清单：{('、'.join(lin['clean_rules'])) or '—'}")
        ap("")
        ap("## 归一 JOIN 清单（REQ-P-027）")
        ap("")
        ap("| 链接 | source → target | ON | 等值归一 | 已声明 |")
        ap("|---|---|---|---|---|")
        for j in self.normalize_joins():
            ap(f"| {j['link']} | {j['source_obj']}.{j['source_prop']} → "
               f"{j['target_obj']}.{j['target_prop']} | {j['on']} | ✓ "
               f"| {'✓' if j['declared'] else '✗'} |")
        ap("")
        ap("## 归一缺口（REQ-P-028）")
        gaps = self.normalize_gaps()
        if gaps is None:
            ap("")
            ap("归一缺口**未计算**（不是「无缺口」）——bindings.json 缺失，无法判定归一状态。")
        elif gaps:
            ap("")
            ap("| 对象.属性 | 说明 |")
            ap("|---|---|")
            for g in gaps:
                ap(f"| {g['object']}.{g['prop']} | {g['note']} |")
        else:
            ap("")
            ap("无缺口：每条 raw 引用均已被某条 build_sql 等值归一（REQ-P-031/032 修复后预期状态）。")
        if self.notes:
            ap("")
            ap("## notes")
            ap("")
            for n in self.notes:
                ap(f"- {n}")
        ap("")
        return "\n".join(lines)

    def render_mermaid(self) -> str:
        broken = self._broken_links()
        inv = {i["name"]: i for i in self.objects_inventory()}
        lines = ["graph LR"]
        for name, o in self.objects.items():
            label = name
            if o.get("title"):
                label += " " + str(o["title"])
            if inv.get(name, {}).get("hidden_hub"):
                label = "★ " + label
            if o.get("runtime"):
                label += "（runtime）"
            lines.append(f'  {name}["{label}"]')
        for lname, l in self.links.items():
            if l.get("runtime"):
                continue
            fo, to = l.get("from_obj") or "", l.get("to_obj") or ""
            if not fo or not to:
                continue
            arrow = "-.->" if lname in broken else "-->"
            lines.append(f'  {fo} {arrow}|"{lname}"| {to}')
        for n in self.notes:
            lines.append(f"%% note: {n}")
        return "\n".join(lines)
