"""
core/validate.py
六段输出结构校验 + 红线自检。
"""

from .hypotheses import MiaoSuan


REQUIRED_SECTIONS = ["庙算基线", "双向盘点", "虚实扫描", "奇正分工", "用间交叉", "全胜校验"]


def validate(output: dict) -> dict:
    errors: list[str] = []
    # 结构校验
    for sec in REQUIRED_SECTIONS:
        if sec not in output:
            errors.append(f"缺少段落：{sec}")
    # 红线1：每条推断可溯源（全胜校验里须有 溯源 字段）
    full = output.get("全胜校验", {})
    if isinstance(full, dict):
        for k, v in full.items():
            if isinstance(v, str) and "溯源" in k and not v:
                errors.append(f"红线：{k} 溯源为空")
    return {"passed": len(errors) == 0, "errors": errors}


def redline_check(miao: MiaoSuan) -> dict:
    """红线自检：知己非空 + 假设受限正确 + AI 未定性。"""
    warnings: list[str] = []
    if not miao.ji:
        warnings.append("红线：知己栏为空")
    for h in miao.hypotheses:
        if "收受财物" in h.description and h.status == "可推演":
            warnings.append("红线：定性类假设不得标记为可推演，应交由正兵")
    return {"passed": len(warnings) == 0, "warnings": warnings}
