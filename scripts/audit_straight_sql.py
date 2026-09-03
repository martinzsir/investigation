"""
scripts/audit_straight_sql.py
直查源表静态扫描（REQ-003 AC5）：扫描 core/ 与 skills/ 下的 .py，
找出经 Store 读接口（query/cold_scan）或 store.execute 直查中文业务源表的绕过调用。

判定：
  - <store>.query(...) / <store>.cold_scan(...)：SQL 首参数含受保护表名标识符
    （字符串字面量内的文件名不算，与运行时守卫同口径）→ 违规；
  - <store>.execute(...) 且 SQL 为 SELECT 且命中受保护表 → 违规
    （execute 是写/DDL 路径，SELECT 源表属绕过）；
  - 原始 conn.execute（编译器/接入层管道，如 ontology.py/registry.py）不在扫描范围
    ——架构约束作用于 Store 公共读接口。

用法：
    python scripts/audit_straight_sql.py                # 打印报告，违规退出码 1
    python scripts/audit_straight_sql.py --fail-on-violation
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "core", ROOT / "skills"]

FORBIDDEN_TABLES = (
    "银行流水", "通话记录", "招投标档案", "工商信息",
    "轨迹出行", "公开OSINT", "举报材料",
)

# 守卫定义自身豁免
ALLOWLIST_FILES = {"store.py"}

READ_METHODS = {"query", "cold_scan"}
EXEC_METHODS = {"execute"}

_STRIP_LITERALS = re.compile(r"'[^']*'")


def _sql_from_node(node: ast.AST) -> str:
    """从调用首参数提取 SQL 文本：常量直取；f-string 拼接常量段；其它返回空。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append(" ")  # 动态段占位（不豁免，也不产生表名误报）
        return "".join(parts)
    return ""


def _touches(sql: str) -> list[str]:
    stripped = _STRIP_LITERALS.sub("''", sql)
    return [t for t in FORBIDDEN_TABLES if t in stripped]


def _is_store_receiver(func: ast.AST) -> bool:
    """<name>.method 或 self.store.method / self._store.method。"""
    if not isinstance(func, ast.Attribute):
        return False
    val = func.value
    if isinstance(val, ast.Name) and val.id in ("store", "s"):
        return True
    if isinstance(val, ast.Attribute) and val.attr in ("store", "_store"):
        return True
    return False


def scan_file(path: Path) -> list[dict]:
    violations = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return [{"file": str(path), "line": 0, "method": "-",
                 "tables": [], "sql": f"(语法错误: {e})"}]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method not in READ_METHODS | EXEC_METHODS:
            continue
        if not _is_store_receiver(node.func):
            continue
        # 取 SQL 文本：
        #   query/execute → 首参即 SQL；
        #   cold_scan → 首参是文件 pattern（文件名含表名属正常，不判），
        #               SQL 片段在 extra_where（第 2 位置参或关键字参）。
        candidates: list[str] = []
        if method == "cold_scan":
            if len(node.args) >= 2:
                candidates.append(_sql_from_node(node.args[1]))
            for kw in node.keywords:
                if kw.arg == "extra_where":
                    candidates.append(_sql_from_node(kw.value))
        else:
            if node.args:
                candidates.append(_sql_from_node(node.args[0]))
        sql = " ".join(c for c in candidates if c)
        if not sql:
            continue
        tables = _touches(sql)
        if not tables:
            continue
        if method in EXEC_METHODS and not re.match(r"\s*SELECT", sql, re.IGNORECASE):
            continue  # execute 的写/DDL 路径放行（INSERT/CREATE/DELETE...）
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            rel = str(path)  # 仓库外文件（单测喂的临时文件）
        violations.append({
            "file": rel,
            "line": node.lineno,
            "method": method,
            "tables": tables,
            "sql": " ".join(sql.split())[:120],
        })
    return violations


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-on-violation", action="store_true",
                    help="有违规则退出码 1（CI 用）")
    args = ap.parse_args()

    all_v: list[dict] = []
    for d in SCAN_DIRS:
        for p in sorted(d.rglob("*.py")):
            if p.name in ALLOWLIST_FILES:
                continue
            all_v.extend(scan_file(p))

    if all_v:
        print(f"❌ 发现 {len(all_v)} 处直查业务源表调用（core/skills 必须走语义层）：")
        for v in all_v:
            print(f"  {v['file']}:{v['line']}  .{v['method']}()  命中 {v['tables']}")
            print(f"      SQL: {v['sql']}")
        return 1 if args.fail_on_violation else 1
    print("✅ 静态扫描通过：core/ 与 skills/ 无 Store 直查业务源表调用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
