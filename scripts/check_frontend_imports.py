#!/usr/bin/env python3
"""前端静态一致性检查（vue-tsc 不可用时的替代手段）。

沙盒 npm 源 403，装不了 typescript / vue-tsc，无法做真正的类型检查。
本脚本做能做的部分——**导入/导出对账**，可抓到大部分低级错误：
  - 导入路径文件不存在（改名/移动后最易漏）
  - 具名导入在目标文件里没有对应导出（新增 API 忘记 export）
  - 导入了但文件里没用到（死导入，多为重构残留）

不替代类型检查：props 类型、泛型、模板内表达式的类型错误仍需 vue-tsc。

用法：python3 scripts/check_frontend_imports.py [前端根路径]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

EXTS = (".ts", ".vue")

# import ... from './x'  |  import './x'  |  export ... from './x'
FROM_RE = re.compile(
    r"""(?:from|import)\s+['"](\.[^'"]+)['"]""")

# import { a, b as c } from 'spec'  —— 具名与来源必须**成对**匹配，
# 否则会拿 A 语句的具名去比 B 语句的目标，产生笛卡尔积式误报。
IMPORT_STMT_RE = re.compile(
    r"""import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""")

# export const/function/interface/type/class/enum NAME
EXPORT_RE = re.compile(
    r"""export\s+(?:declare\s+)?(?:const|let|var|function|async\s+function|"""
    r"""class|interface|type|enum)\s+([A-Za-z_$][\w$]*)""")
# export { a, b }  /  export type { a, b }（re-export 常见形式）
EXPORT_BRACE_RE = re.compile(r"""export\s+(?:type\s+)?\{([^}]*)\}""")


def resolve(base: Path, spec: str) -> Path | None:
    """解析相对导入到实际文件（补 .ts/.vue/index.ts）。

    注意：不能用 with_suffix —— 对 `./fetch.transport` 会误判成 `fetch.ts`
    （带点号的模块名很常见）。应追加后缀而非替换。
    """
    p = (base.parent / spec).resolve()
    if p.is_file():
        return p
    for cand in (Path(str(p) + ".ts"), Path(str(p) + ".vue"),
                 p / "index.ts", p / "index.vue"):
        if cand.is_file():
            return cand
    return None


def exported_names(path: Path) -> set[str]:
    """提取文件的导出名（够用即可：export X + export {X}）。"""
    try:
        t = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    names: set[str] = set(EXPORT_RE.findall(t))
    for grp in EXPORT_BRACE_RE.findall(t):
        for item in grp.split(","):
            item = item.strip()
            if not item:
                continue
            # export { a as b } → 对外可见名是 b
            names.add(item.split(" as ")[-1].strip())
    # export default
    if re.search(r"""export\s+default""", t):
        names.add("default")
    return {n for n in names if n}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "frontend/src").resolve()
    if not root.is_dir():
        print(f"路径不存在：{root}")
        return 2

    files = [p for p in root.rglob("*") if p.suffix in EXTS and p.is_file()]
    errors: list[str] = []
    warns: list[str] = []

    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # 去掉注释块再做匹配（避免注释里的示例导入误报）
        body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)

        # ① 缺失文件检查：所有 from '...' 的相对路径
        for spec in FROM_RE.findall(body):
            if spec.endswith((".css", ".scss", ".json")):
                continue
            if resolve(f, spec) is None:
                errors.append(
                    f"[缺失文件] {f.relative_to(root)}: 导入 '{spec}' 解析不到文件")

        # ② 具名导入对账（具名与来源成对匹配）
        for grp, spec in IMPORT_STMT_RE.findall(body):
            if not spec.startswith("."):
                continue  # 裸模块名（naive-ui/vue）无法本地解析
            target = resolve(f, spec)
            if target is None:
                continue  # 已在 ① 报缺失文件
            available = exported_names(target)
            if not available:
                continue  # 目标无显式导出（如 .vue SFC）→ 跳过
            for item in grp.split(","):
                item = item.strip()
                if not item or item.startswith("type "):
                    continue
                name = item.split(" as ")[0].strip()
                if not name:
                    continue
                if name not in available:
                    errors.append(
                        f"[导出缺失] {f.relative_to(root)}: "
                        f"从 '{spec}' 导入 '{name}'，但目标未导出"
                        f"（可用：{sorted(available)[:8]}"
                        f"{'…' if len(available) > 8 else ''}）")

        # ③ 死导入粗查：导入名在正文出现次数 ≤1（仅出现在 import 行）
        #    排除 type-only 导入（类型名在模板/注解里常只出现一次，且
        #    `import type` 不影响运行时，不构成死代码）。
        for grp, _spec in IMPORT_STMT_RE.findall(body):
            for item in grp.split(","):
                raw = item.strip()
                if not raw or raw.startswith("type "):
                    continue  # import { type X } 形式
                name = raw.split(" as ")[-1].strip()
                if not name:
                    continue
                occurrences = len(re.findall(r"\b" + re.escape(name) + r"\b", body))
                if occurrences <= 1:
                    warns.append(
                        f"[疑似死导入] {f.relative_to(root)}: '{name}' 导入后未使用")

    print(f"扫描 {len(files)} 个前端文件（.ts/.vue）")
    print()
    if errors:
        print(f"❌ 错误 {len(errors)} 项：")
        for e in errors:
            print("   " + e)
    else:
        print("✅ 导入/导出对账通过（无缺失文件、无未导出引用）")

    if warns:
        print()
        print(f"⚠️  提示 {len(warns)} 项（不阻断）：")
        for w in warns[:20]:
            print("   " + w)
        if len(warns) > 20:
            print(f"   …另有 {len(warns) - 20} 项")

    print()
    print("注意：本检查不替代 vue-tsc —— props 类型、泛型、模板表达式类型仍需真正类型检查。")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
