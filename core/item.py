"""
core/item.py —— 重点物品身份与凭证（R3 / R6 / R14 / R15 / D10 / D4）。

三条设计红线，改动前务必读：

1. **标识符只存摘要，不落明文**（R14）。`build_identifier()` 的返回值里
   没有 `value` 字段——缓存、日志、观察摘要一律只见到 `value_digest`。
   需要原文时走授权读取路径，不在本模块内传递明文。

2. **无凭证物品不静默合并**（D4）。`kind="none"` 类（赃物、管制物品）没有
   唯一凭证，若让它们的 `primary_digest` 统一为空串，语义层去重会把所有
   无凭证物品坍缩成**一个**实体——比不消歧更危险。故取 descriptors 摘要，
   天然各成实体。宁可分列，不可误合。

3. **组织凭证判定是条件式的，不能写成静态 identity_key**（D10）。
   写 `["raw_name","credit_code"]` 会让更名主体（同码不同名）被误分列；
   只写 `["credit_code"]` 会让当前无码数据全数坍缩为一个实体。
   故由 `org_identity_key()` 在代码层按"有码用码、无码用名"判定。

校验位一律软判定：格式或校验位不合法只标 `verified=False` 并给出原因，
绝不硬失败——一条脏数据不应拖垮整批装载（D10 / D4 同源）。

词汇声明化（Palantir 策略：代码引用 apiName，人读 displayName，词汇映射
是本体数据）：本模块全部词汇常量（ITEM_IDENTIFIER_KINDS / ITEM_KIND_LABELS /
IDENTIFIER_KIND_ALIASES / SENSITIVE_IDENTIFIER_KINDS / ITEM_TYPE_SOURCE_MAP /
ITEM_TYPE_LABELS）在 import 期从本体声明派生——真源是
``ontology/_shared/data_elements.json`` 的 DE_ITEM_TYPE 与
DE_ITEM_IDENTIFIER_KIND 两个数据元的 ``enum_meta``，装载与交叉校验在
``core/ontology_loader.load_item_vocabulary()``。**别在代码里再写一份**：
散在两处迟早分叉，而分叉的症状是同一件车被存成两种凭证，两条链都对不上。
改词汇（加种类/加别名/调标签）改 JSON，不改本文件。
"""
from __future__ import annotations

import hashlib
import json

from core.data_elements import norm_vocab_alias
from core.ontology_loader import load_item_vocabulary

# 归一化比较键（去空格/下划线/连字符并小写）。实现单源在 core/data_elements.py
# ——声明层建索引与查询层归一必须同口径，各写一份会让"声明说没抢名、查询却
# 命中另一条"。模块内保留 _norm_alias 名字，既有调用与测试无感。
_norm_alias = norm_vocab_alias

# import 期装载：声明缺失/抢名/悬空引用 → 硬失败（宁崩不歧义），
# 任何用到物品词汇的进程在启动即爆，不带病运行。
_VOCAB = load_item_vocabulary()

# ---- 标识符种类（R3）----
# none 仅用于 D4 类物品：无唯一凭证，只作描述性实体。
# 顺序即 primary_digest 选取优先级（声明序，勿在代码层重排）。
ITEM_IDENTIFIER_KINDS: tuple[str, ...] = _VOCAB["identifier_kinds"]

# 属个人信息的标识符种类（R13）：需按敏感口径遮蔽，且明文不可用于关联。
SENSITIVE_IDENTIFIER_KINDS: frozenset = _VOCAB["sensitive_identifier_kinds"]

# ---- 中文源表物品类型 → (item_type, 标识符种类) ----
# 真源：DE_ITEM_TYPE.enum_meta.*.source_labels + identifier_kind。
# 消费方：scripts/prep_item_registry.py（预装配）、画布物品层（展示回译）。
ITEM_TYPE_SOURCE_MAP: dict[str, tuple[str, str]] = _VOCAB["item_type_source_map"]

# 物品类型代码 → 规范中文名（界面显示；无 label 的枚举值回落代码本身）。
ITEM_TYPE_LABELS: dict[str, str] = _VOCAB["item_type_labels"]

# 有凭证种类按此顺序取主标识，保证同批数据选取稳定（不随列表顺序漂移）。
_PRIMARY_ORDER = {k: i for i, k in enumerate(ITEM_IDENTIFIER_KINDS)}


# ---- 种类代码 ⇄ 中文标签/别名（界面登记入口）----
# 正兵在界面上填的是中文（"车牌号"），入库必须是代码（"plate"）。
# 这张表是唯一的归一入口：**别在界面或路由里各写一份 if/elif**。
# 真源：DE_ITEM_IDENTIFIER_KIND.enum_meta.*.label / aliases。
ITEM_KIND_LABELS: dict[str, str] = _VOCAB["identifier_kind_labels"]

# 归一化别名 → 种类代码（代码本身/label/别名全量参与建索引；
# 抢名已在装载期硬失败）。别名只增不删：删掉一个别名会让旧输入静默落进 None。
IDENTIFIER_KIND_ALIASES: dict[str, str] = _VOCAB["identifier_kind_aliases"]


def normalize_identifier_kind(label: Any) -> str | None:
    """中文标签／别名 → 种类代码；识别不了返回 **None**（不猜）。

    与"重名不自裁"同源：猜错比不认更坏。填"车号"这种表外说法时，
    宁可让正兵从下拉里选，也不能静默落到 plate 或 none——
    落到 none 会把一件有凭证的车记成无凭证赃物（D4 类），
    后续永不参与消歧，等于永久丢实体。
    """
    s = str(label or "").strip()
    if not s:
        return None
    # 索引已含代码本身与规范标签，一次命中；识别不了 None。
    return IDENTIFIER_KIND_ALIASES.get(_norm_alias(s))


def identifier_kind_label(kind: Any) -> str:
    """种类代码 → 规范中文标签；未知种类原样返回，不编造。"""
    k = str(kind or "").strip()
    return ITEM_KIND_LABELS.get(k) or k


def digest_of(value: str | None, *, kind: str = "") -> str:
    """标识符摘要口径（R14 唯一真相源）。

    - 空值返回空串：绝不把"没有凭证"摘要成某个具体串，否则所有无凭证项
      会共享同一个 digest 而被误合并。
    - 摘要串带 kind 前缀：防止不同种类的同形值（如 serial 与 plate 同串）
      跨界碰撞。 join 仅在同一 kind 内进行，故不影响匹配。
    """
    s = (value or "").strip()
    if not s:
        return ""
    return hashlib.sha256(f"{kind}|{s}".encode("utf-8")).hexdigest()


def build_identifier(kind: str, value: str | None, *, issuer: str = "",
                     verified: bool | None = None) -> dict:
    """构造标识符条目（R3）。

    返回结构 `{kind, value_digest, sensitive, issuer, verified}`——**无明文字段**。
    `sensitive` 按种类判定（msisdn/imei 属个人信息），不靠调用方记得传。
    """
    if kind not in ITEM_IDENTIFIER_KINDS:
        raise ValueError(
            f"未知标识符种类：{kind!r}（允许 {list(ITEM_IDENTIFIER_KINDS)}）")
    return {
        "kind": kind,
        "value_digest": digest_of(value, kind=kind),
        "sensitive": kind in SENSITIVE_IDENTIFIER_KINDS,
        "issuer": issuer,
        "verified": verified,
    }


def primary_digest(identifiers: list[dict] | None,
                   descriptors: dict | None = None) -> str:
    """主标识摘要（R6 复合身份键的一半）。

    - 有凭证：按种类优先级取第一个非空 digest（顺序稳定）。
    - 无凭证（全为 none 或为空）：取 descriptors 的规范摘要 → 每条记录各异，
      从而"各成实体、不静默合并"（D4）。descriptors 亦为空时返回空串，
      由调用方按"无身份"处理，不得当作可合并键。
    """
    cands = [i for i in (identifiers or [])
             if isinstance(i, dict) and i.get("kind") != "none"
             and i.get("value_digest")]
    if cands:
        cands.sort(key=lambda i: _PRIMARY_ORDER.get(i.get("kind"), 99))
        return cands[0]["value_digest"]
    if descriptors:
        blob = json.dumps(descriptors, ensure_ascii=False, sort_keys=True,
                          default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return ""


def verify_credit_code(code: str | None) -> tuple[bool, str]:
    """统一社会信用代码软校验（R15 / D10）。

    返回 `(verified, reason)`，**永不抛异常**：
    - 未提供 → (False, "未提供统一社会信用代码")——沿用 D4 精神，不进自动消歧
    - 长度/字符集不符 → (False, "格式不符")
    - 校验位不符 → (False, "校验位不合法")
    - 通过 → (True, "")
    标 unverified 不硬失败：脏数据不该拖垮整批装载。
    """
    from core.data_elements import checksum_credit_code_mod31
    s = (code or "").strip().upper()
    if not s:
        return False, "未提供统一社会信用代码"
    if len(s) != 18:
        return False, "格式不符"
    if not checksum_credit_code_mod31(s):
        return False, "校验位不合法"
    return True, ""


def org_identity_key(raw_name: str | None,
                     credit_code: str | None) -> tuple[str, str]:
    """组织身份键：有码用码、无码用名（D10）。

    返回 `(basis, key)`，basis ∈ {"credit_code", "raw_name"}。
    调用方须把 basis 一并记入诊断——"按名合并"与"按码认定同一主体"
    是两种不同强度的结论，不能同日而语。
    """
    code = (credit_code or "").strip().upper()
    if code:
        return "credit_code", code
    return "raw_name", (raw_name or "").strip()
