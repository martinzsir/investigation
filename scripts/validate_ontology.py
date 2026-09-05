"""
scripts/validate_ontology.py
装载期 + JSON Schema 双校验（REQ-019）。

用法：
  python scripts/validate_ontology.py              # 普通校验，警告不退出
  python scripts/validate_ontology.py --strict       # 严格模式，任何错误退出码 1
  python scripts/validate_ontology.py --pack <包名>  # 指定案件包
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from jsonschema import Draft7Validator  # noqa: E402


SCHEMA_FILES = ["objects", "links", "bindings", "actions", "functions",
                "rules", "policies", "views"]


def validate_schema(instance_path: Path, schema_path: Path) -> list[str]:
    """用 jsonschema 校验单个 JSON 文件，返回错误信息列表。"""
    if not instance_path.exists():
        return [f"实例文件不存在：{instance_path}"]
    if not schema_path.exists():
        return [f"schema 文件不存在：{schema_path}"]
    try:
        with open(instance_path, encoding="utf-8") as f:
            instance = json.load(f)
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        return [f"JSON 解析失败 {instance_path.name}：{e}"]
    errors = []
    for err in Draft7Validator(schema).iter_errors(instance):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{instance_path.name}#{path}: {err.message}")
    return errors


def validate_ontology(pack: str = "default") -> list[str]:
    """对 ontology/<pack>/ 下六个 JSON 做 schema 校验 + loader 交叉校验。"""
    errors: list[str] = []
    # 1. JSON Schema 校验
    for name in SCHEMA_FILES:
        instance = ROOT / "ontology" / pack / f"{name}.json"
        schema = ROOT / "schemas" / f"{name}.schema.json"
        errors.extend(validate_schema(instance, schema))
    # 2. 复用 ontology_loader.load_pack 做交叉引用校验
    try:
        from core.ontology_loader import load_pack
        load_pack(pack)
    except (ValueError, FileNotFoundError) as e:
        errors.append(f"load_pack({pack}) 交叉校验失败：{e}")
    except Exception as e:
        errors.append(f"load_pack({pack}) 未预期异常：{type(e).__name__}: {e}")
    return errors


def main():
    ap = argparse.ArgumentParser(description="Ontology JSON Schema + 交叉引用校验")
    ap.add_argument("--strict", action="store_true", help="任何错误退出码 1")
    ap.add_argument("--pack", default="default", help="案件包名（默认 default）")
    args = ap.parse_args()
    errors = validate_ontology(args.pack)
    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1 if args.strict else 0)
    print("✅ ontology 校验通过")


if __name__ == "__main__":
    main()
