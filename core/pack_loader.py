"""
core/pack_loader.py
镜头包自枚举器（P3）：扫描 packs/*/pack.json，统一注册到 SkillRegistry。

设计（见 .trae/documents/研判能力插件化_实施方案_v3.md §3.6）：
  枚举（发现 pack.json）→ 注册（SkillSpec 登记、校验引用底座对象/链接存在、
  params_schema 合法）→ 挂载（进入调度目录）→ 可被 skill_invoke 调度。
  - pack 内实现代码放 pack 目录（如 impl.py），pack.json 以 "impl:func_name"
    引用，importlib 按绝对路径安全加载；接入第 N 个镜头 = 放一个 pack 目录，
    零注册代码。
  - 单包声明/实现非法只拒该包（failed 清单），不连坐其余包。
  - 重复 discover 对账拔出：目录已消失的枚举包技能自动注销；
    pack_id="_builtin" 的内置技能（registry_bootstrap）不动。

pack.json 结构：
{
  "pack_id": "relation",
  "version": "0.1.0",
  "skills": [
    {
      "skill_id": "relation_circle",
      "name": "关系圈层镜头",
      "stage": "用间",
      "consumes_objects": ["org", "person", "owns"],
      "produces_dims": ["关系"],
      "mode": "deterministic",
      "enabled": true,
      "params_schema": {"depth": {"type": "integer", "required": true}},
      "scope": {"reads": ["org", "person", "owns"]},
      "handler": "impl:circle"
    }
  ]
}

handler 契约同 skill_invoke：
    handler(miao=None, store=None, ctx=None, params=None, health=None)
        -> [LineageClue]
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from core.registry import SkillRegistry, SkillSpec, get_registry

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKS_DIR = ROOT / "packs"
BUILTIN_PACK_ID = "_builtin"


# ----------------------------------------------------------------------
# 底座类型目录
# ----------------------------------------------------------------------

def ontology_type_names(ontology_pack: str = "default") -> set[str]:
    """已声明的底座对象/链接类型名（注册期引用存在性校验用）。"""
    from core.ontology_loader import load_pack
    p = load_pack(ontology_pack)
    return {o.name for o in p.objects} | {l.name for l in p.links}


# ----------------------------------------------------------------------
# 包实现加载（"module:function"，按绝对路径安全加载）
# ----------------------------------------------------------------------

def _load_handler(pack_dir: Path, ref: str, pack_id: str):
    module_name, sep, func_name = ref.partition(":")
    if not sep or not module_name or not func_name:
        raise ValueError(
            f"包 {pack_id} handler={ref!r} 非法，应为 'module:function'")
    if "/" in module_name or "\\" in module_name or ".." in module_name:
        raise ValueError(
            f"包 {pack_id} handler 模块名 {module_name!r} 含非法路径片段")
    mod_path = (pack_dir / f"{module_name}.py").resolve()
    if mod_path.parent != pack_dir.resolve() or not mod_path.is_file():
        raise FileNotFoundError(
            f"包 {pack_id} handler 模块不存在：{mod_path}")
    mod_id = f"_sunzi_pack_{pack_id}_{module_name}"
    spec = importlib.util.spec_from_file_location(mod_id, mod_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"包 {pack_id} 无法加载模块 {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_id] = mod
    pkg_dir = str(pack_dir)
    added = pkg_dir not in sys.path
    if added:
        sys.path.insert(0, pkg_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        if added:
            try:
                sys.path.remove(pkg_dir)
            except ValueError:
                pass
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        raise AttributeError(
            f"包 {pack_id} 模块 {module_name}.py 中无可调用对象 {func_name}")
    return fn


# ----------------------------------------------------------------------
# 声明 → SkillSpec
# ----------------------------------------------------------------------

def _build_vocabulary(decl: dict[str, Any],
                      pack_dir: Path) -> list[tuple[str, Any]]:
    """解析 provides.vocabulary 声明 → 待注册的 (ontology_pack, 词汇实例) 列表。

    P6：目前仅支持 type="wujian"（五间词汇）。词汇实例由各自运行时模块
    自校验（注册期硬失败，随该包进 failed 清单、不连坐）。
    返回空列表表示该包不提供词汇。
    """
    provides = decl.get("provides")
    if not provides:
        return []
    voc = provides.get("vocabulary")
    if not voc:
        return []
    if not isinstance(voc, dict):
        raise ValueError(f"包 {decl.get('pack_id')} provides.vocabulary 必须为对象")
    vtype = voc.get("type")
    if vtype == "wujian":
        from core.wujian import build_wujian
        onto_pack = str(voc.get("ontology_pack") or "default")
        jians_file = str(voc.get("jians") or "jians.json")
        wp = build_wujian(pack_dir, ontology_pack=onto_pack,
                          jians_file=jians_file)
        return [(onto_pack, wp)]
    raise ValueError(
        f"包 {decl.get('pack_id')} 提供了未知 vocabulary.type={vtype!r}")


def _build_specs(decl: dict[str, Any], pack_dir: Path,
                 ontology_names: set[str]) -> list[SkillSpec]:
    pack_id = decl.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("pack.json 缺少合法 pack_id")
    raw_skills = decl.get("skills")
    if not isinstance(raw_skills, list):
        raise ValueError(f"包 {pack_id} 的 skills 必须为数组")

    specs: list[SkillSpec] = []
    for s in raw_skills:
        if not isinstance(s, dict):
            raise ValueError(f"包 {pack_id} skills 条目必须为对象")
        sid = s.get("skill_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError(f"包 {pack_id} 存在缺少 skill_id 的镜头声明")
        consumes = list(s.get("consumes_objects") or [])
        scope_reads = list((s.get("scope") or {}).get("reads") or [])
        # 注册期引用存在性：引用不存在的底座类型 → 该包拒绝挂载
        for t in (*consumes, *scope_reads):
            if t not in ontology_names:
                raise ValueError(
                    f"包 {pack_id} 镜头 {sid} 引用了不存在的底座类型 {t!r}"
                    f"（对象/链接均无此声明）")
        handler = None
        href = s.get("handler")
        if href:
            handler = _load_handler(pack_dir, href, pack_id)
        spec = SkillSpec(
            skill_id=sid,
            name=str(s.get("name") or sid),
            stage=str(s.get("stage") or "用间"),
            consumes_jian=list(s.get("consumes_jian") or []),
            data_deps=list(s.get("data_deps") or []),
            consumes_objects=consumes,
            produces_dims=list(s.get("produces_dims") or []),
            mode=str(s.get("mode") or "deterministic"),
            enabled=bool(s.get("enabled", True)),
            params_schema=dict(s.get("params_schema") or {}),
            external_services=list(s.get("external_services") or []),
            timeout_ms=int(s.get("timeout_ms", 0) or 0),
            result_ttl_s=int(s.get("result_ttl_s", 0) or 0),
            scope_reads=scope_reads,
            handler=handler,
            pack_id=pack_id,
        )
        spec.validate()  # 提前抛错，错误随该包进 failed 清单
        specs.append(spec)
    return specs


# ----------------------------------------------------------------------
# 自枚举入口
# ----------------------------------------------------------------------

def discover(registry: SkillRegistry | None = None,
             packs_dir: str | Path | None = None,
             ontology_pack: str = "default") -> dict[str, Any]:
    """
    扫描 packs/*/pack.json 并注册镜头；单包失败只拒该包、不连坐。

    返回：
      {"loaded": [pack_id...],          # 本次成功挂载的包
       "failed": [{"path","error"}...], # 声明/实现非法被拒的包
       "removed": [skill_id...]}        # 目录拔出后对账注销的镜头
    幂等：对同一目录重复 discover 即刷新；枚举包拔出后技能自动消失，
    pack_id="_builtin" 的内置技能不受影响。
    """
    reg = registry or get_registry()
    pdir = Path(packs_dir) if packs_dir else DEFAULT_PACKS_DIR
    names = ontology_type_names(ontology_pack)
    result: dict[str, Any] = {"loaded": [], "failed": [], "removed": []}
    seen_skills: set[str] = set()
    seen_vocab: set[str] = set()

    if pdir.is_dir():
        for pack_dir in sorted(p for p in pdir.iterdir() if p.is_dir()):
            pack_json = pack_dir / "pack.json"
            if not pack_json.is_file():
                continue
            try:
                decl = json.loads(pack_json.read_text(encoding="utf-8"))
                specs = _build_specs(decl, pack_dir, names)
                vocab = _build_vocabulary(decl, pack_dir)
                pack_id = str(decl.get("pack_id"))
                for spec in specs:
                    if spec.skill_id in seen_skills:
                        raise ValueError(
                            f"镜头 {spec.skill_id} 与已加载包的 ID 冲突")
                    reg.unregister(spec.skill_id)  # 幂等刷新（同包重扫）
                    reg.register(spec)
                    seen_skills.add(spec.skill_id)
                for onto_pack, instance in vocab:
                    _register_vocabulary(onto_pack, instance)
                    seen_vocab.add(onto_pack)
                # 纯词汇包（无 skills）也算成功挂载
                if specs or vocab:
                    result["loaded"].append(pack_id)
            except Exception as e:
                result["failed"].append({
                    "path": str(pack_json),
                    "error": f"{type(e).__name__}: {e}"})

    # 拔出对账：上次枚举挂载、本次目录已消失/被拒的技能注销（内置不动）
    for spec in reg.all_specs():
        if (spec.pack_id != BUILTIN_PACK_ID
                and spec.skill_id not in seen_skills):
            reg.unregister(spec.skill_id)
            result["removed"].append(spec.skill_id)
    # 词汇拔出对账：本次未提供的 ontology 包词汇注销
    for onto_pack in _offered_vocabulary() - seen_vocab:
        _unregister_vocabulary(onto_pack)
    return result


def _register_vocabulary(onto_pack: str, instance: Any) -> None:
    """把词汇实例注册到对应运行时注册表（按 vocabulary 类型分派）。"""
    from core.wujian import WujianPack, register_wujian
    if isinstance(instance, WujianPack):
        register_wujian(instance)
        return
    raise ValueError(f"未知词汇实例类型：{type(instance)}")


def _unregister_vocabulary(onto_pack: str) -> None:
    from core.wujian import unregister_wujian
    unregister_wujian(onto_pack)


def _offered_vocabulary() -> set[str]:
    """各词汇运行时当前已注册的 ontology 包并集。"""
    from core.wujian import offered_packs
    return offered_packs()
