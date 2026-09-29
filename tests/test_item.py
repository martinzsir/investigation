"""
tests/test_item.py —— 物品对象本体层与身份层（R1–R6 / R13–R15 / D4 / D10 / D11）。

覆盖测试编号：T1 T2 T3 T4 T5 T6 T11 T12 T27 T28 T29 T31 T32 T33 T34
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _objects(pack: str = "default") -> dict:
    import json as _j
    d = _j.loads((ROOT / "ontology" / pack / "objects.json").read_text(encoding="utf-8"))
    return {o["name"]: o for o in d["objects"]}


def _policies(pack: str = "default") -> dict:
    import json as _j
    return _j.loads((ROOT / "ontology" / pack / "policies.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# T1 / T2：item 对象类型声明
# ----------------------------------------------------------------------
class TestT1ItemDeclared:
    def test_t1_item_in_objects(self):
        """T1：item 已入本体，且为实体型、带主键与身份属性。"""
        from core.ontology_loader import load_pack
        types = {o.name: o for o in load_pack("default").objects}
        assert "item" in types
        it = types["item"]
        assert it.kind == "entity"
        assert it.name_property == "title"
        for p in ("item_type", "title", "identifiers", "primary_digest"):
            assert p in it.properties, f"缺属性 {p}"

    def test_t2_item_has_binding_and_policy(self):
        """T2：非 runtime 对象必须有 binding，且必须有对象级策略（AC5 覆盖）。"""
        import json as _j
        b = _j.loads((ROOT / "ontology/default/bindings.json").read_text(encoding="utf-8"))
        assert any(x["object"] == "item" for x in b["object_bindings"])
        names = [o["object"] for o in _policies()["object_policies"]]
        assert "item" in names


# ----------------------------------------------------------------------
# T3 / T4：item_type 枚举
# ----------------------------------------------------------------------
class TestT3ItemTypeEnum:
    def test_t3_enum_values_declared(self):
        """T3：enum 属性必须带 enum_values 白名单（REQ-041 AC2），且含通用取值。"""
        it = _objects()["item"]
        assert it["properties"]["item_type"]["data_element"] == "DE_ITEM_TYPE"
        allowed = it["enum_values"]["item_type"]
        for v in ("vehicle", "realestate", "invoice", "drug_batch", "goods_other"):
            assert v in allowed, f"通用枚举缺 {v}"

    def test_t4_enum_without_whitelist_hard_fails(self, tmp_path):
        """T4：enum 属性未声明 enum_values → 装载期硬失败（不静默放行）。"""
        from core.ontology_loader import _load_objects
        bad = {"schema_version": 2, "objects": [{
            "name": "item", "pk": "item_id", "name_property": "title",
            "kind": "entity",
            "properties": {"item_type": "enum", "title": "string"},
        }]}
        p = tmp_path / "objects.json"
        p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="enum_values"):
            _load_objects(p, {}, set())


# ----------------------------------------------------------------------
# T5 / T6：identifiers 结构
# ----------------------------------------------------------------------
class TestT5Identifiers:
    def test_t5_multi_identifier_no_plaintext(self):
        """T5：同一物品可挂多标识符，且返回值不含任何明文字段。"""
        from core.item import build_identifier
        ids = [
            build_identifier("plate", "浙A12345", issuer="车管所", verified=True),
            build_identifier("imei", "356938035643809", verified=None),
        ]
        assert ids[0]["value_digest"] != ids[1]["value_digest"]
        for i in ids:
            assert "value" not in i and "raw" not in i
            assert set(i) == {"kind", "value_digest", "sensitive", "issuer", "verified"}
        # 敏感位按种类自动判定，不靠调用方记得传
        assert ids[1]["sensitive"] is True and ids[0]["sensitive"] is False

    def test_t5_digest_stable_and_kind_scoped(self):
        """T5 附：同值同 kind 摘要稳定；同值不同 kind 不碰撞（防跨界）。"""
        from core.item import digest_of
        assert digest_of("浙A12345", kind="plate") == digest_of("浙A12345", kind="plate")
        assert digest_of("ABC", kind="plate") != digest_of("ABC", kind="serial")

    def test_t6_none_kind_not_deduped(self):
        """T6：kind=none 的物品不参与消歧——各自成实体，绝不共享同一个键。"""
        from core.item import build_identifier, primary_digest
        a = build_identifier("none", None)
        b = build_identifier("none", None)
        assert a["value_digest"] == "" and b["value_digest"] == ""
        da = primary_digest([a], {"特征": "银色金属U盘", "发现地": "A室"})
        db = primary_digest([b], {"特征": "银色金属U盘", "发现地": "B室"})
        assert da and db and da != db, "无凭证物品不得被合并为同一实体"

    def test_t6_empty_descriptors_returns_empty(self):
        """T6 附：既无凭证又无 descriptors → 空串，由调用方按'无身份'处理。
        绝不生成一个共享常量 digest，否则等价于把所有无身份物品合成一个。"""
        from core.item import primary_digest
        assert primary_digest([], None) == ""
        assert primary_digest([{"kind": "none", "value_digest": ""}], {}) == ""


# ----------------------------------------------------------------------
# T11 / T12：复合身份键
# ----------------------------------------------------------------------
class TestT11IdentityKey:
    def test_t11_composite_key(self):
        """T11：item.identity_key = [item_type, primary_digest]，与 person_identity 同机制。"""
        it = _objects()["item"]
        assert it["identity_key"] == ["item_type", "primary_digest"]

    def test_t12_no_counterevidence_keeps_merged(self):
        """T12：同牌号无反证 → 保持合并并留诊断，不静默拆分。"""
        from core.item import build_identifier, primary_digest
        x = primary_digest([build_identifier("plate", "浙A12345")], {"颜色": "白"})
        y = primary_digest([build_identifier("plate", "浙A12345")], {"颜色": "黑"})
        assert x == y, "同牌号且无判别证据时不得拆分（拆分须有人证/反证）"


# ----------------------------------------------------------------------
# T27 / T28 / T34：敏感与遮蔽（fail-closed）
# ----------------------------------------------------------------------
class TestT27Sensitive:
    def test_t34_phone_sensitive_declared(self):
        """T34：DE_PHONE 已标 sensitive，且每个引用它的属性都已声明遮蔽。"""
        import json as _j
        els = _j.loads((ROOT / "ontology/_shared/data_elements.json").read_text(encoding="utf-8"))["elements"]
        assert els["DE_PHONE"].get("sensitive") is True
        for pack in ("default", "reqd_case"):
            objs = _objects(pack)
            pol = _policies(pack)
            masked = {(p["object"], p["property"]) for p in pol["property_policies"]}
            for name, o in objs.items():
                for prop, t in (o.get("properties") or {}).items():
                    if isinstance(t, dict) and t.get("data_element") == "DE_PHONE":
                        assert (name, prop) in masked, (
                            f"{pack}: {name}.{prop} 引用敏感数据元但未声明遮蔽")

    def test_t27_undeclared_mask_hard_fails(self, tmp_path):
        """T27：sensitive 数据元未在 policies 声明遮蔽 → 装载期硬失败。"""
        from core.ontology_loader import _load_objects
        els = {"DE_X": {"name": "x", "type": "string", "sensitive": True}}
        bad = {"schema_version": 2, "objects": [{
            "name": "o", "pk": "o_id", "name_property": "n", "kind": "entity",
            "properties": {"n": "string", "s": {"data_element": "DE_X"}},
        }]}
        p = tmp_path / "objects.json"
        p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match=r"未在 policies\.json property_policies 声明遮蔽"):
            _load_objects(p, els, set())

    def test_t28_masked_but_role_can_read(self):
        """T28：已声明遮蔽 → 授权角色可取原文，其余角色得到遮蔽串。"""
        from core.access import AccessContext
        from core.policy import PolicyEngine
        pe = PolicyEngine("default")
        rows = [{"phone": "13812345678"}]
        host = pe.apply_row_masks(
            AccessContext(role="主办", clearance=9, operator="t"), "person_identity", rows)
        assert host[0]["phone"] == "13812345678"
        zb = pe.apply_row_masks(
            AccessContext(role="正兵", clearance=1, operator="t"), "person_identity", rows)
        assert zb[0]["phone"] != "13812345678" and "*" in zb[0]["phone"]

    def test_t28_join_must_not_use_masked_plaintext(self):
        """T28 附（I23 前提）：遮蔽后的明文串不可作 join 键——不同人的号码会
        被遮成同一个串而错配。join 一律走 digest。"""
        from core.item import digest_of
        masked_a = "138****5678"
        masked_b = "139****5678"
        assert masked_a != masked_b  # 本例恰好不同，但…
        # 反例：前 3 后 4 相同的两个号码被遮成同一串
        assert "138****5678" == "138****5678"
        assert digest_of("13800005678", kind="msisdn") != digest_of("13811115678", kind="msisdn")


# ----------------------------------------------------------------------
# T29：摘要口径不落明文
# ----------------------------------------------------------------------
class TestT29NoPlaintext:
    def test_t29_identifier_output_has_no_plaintext(self):
        """T29：物品模块输出的任何结构都不得出现明文标识符。"""
        from core.item import build_identifier
        raw = "浙A12345"
        i = build_identifier("plate", raw)
        blob = json.dumps(i, ensure_ascii=False)
        assert raw not in blob
        assert i["value_digest"]

    def test_t29_digest_of_empty_is_empty(self):
        """T29 附：空值摘要为空串，绝不生成共享常量。"""
        from core.item import digest_of
        assert digest_of("") == "" and digest_of(None) == "" and digest_of("   ") == ""


# ----------------------------------------------------------------------
# T31 / T32 / T33：组织凭证（R15 / D10）
# ----------------------------------------------------------------------
def _valid_credit_code() -> str:
    """按 GB 32100-2015 现算一个校验位合法的代码（避免手写错值）。"""
    charset = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    weights = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
    body = "91350100M000100Y4"
    s = sum(charset.index(c) * w for c, w in zip(body, weights))
    check = 31 - (s % 31)
    return body + charset[0 if check == 31 else check]


class TestT31OrgCreditCode:
    def test_t31_same_code_diff_name_is_one_org(self):
        """T31：同码不同名（更名）→ 同一主体，不得误分列。"""
        from core.item import org_identity_key
        b1, k1 = org_identity_key("宏业建设", "91350100M000100Y43")
        b2, k2 = org_identity_key("宏业建设集团", "91350100M000100Y43")
        assert (b1, k1) == (b2, k2) and b1 == "credit_code"

    def test_t31_same_name_diff_code_is_two_orgs(self):
        """T31 附：同名不同码 → 两个主体，靠码区分而非名称。"""
        from core.item import org_identity_key
        assert org_identity_key("宏业建设", "AAA")[1] != org_identity_key("宏业建设", "BBB")[1]

    def test_t32_no_code_falls_back_to_name(self):
        """T32：无码组织沿用 D4 精神——不进自动消歧，按名保持合并并留诊断。"""
        from core.item import org_identity_key
        basis, key = org_identity_key("宏业建设", None)
        assert basis == "raw_name" and key == "宏业建设"
        # basis 必须返回，便于调用方记"按名合并"的诊断（强度弱于按码认定）

    def test_t33_bad_checksum_unverified_not_hard_fail(self):
        """T33：校验位非法 → 标 unverified 并给出原因，绝不硬失败。"""
        from core.item import verify_credit_code
        good = _valid_credit_code()
        ok, why = verify_credit_code(good)
        assert ok is True and why == ""
        bad = good[:-1] + ("Z" if good[-1] != "Z" else "Y")
        ok2, why2 = verify_credit_code(bad)
        assert ok2 is False and why2 == "校验位不合法"
        ok3, why3 = verify_credit_code("123")
        assert ok3 is False and why3 == "格式不符"
        ok4, why4 = verify_credit_code("")
        assert ok4 is False and why4 == "未提供统一社会信用代码"

    def test_t33_credit_code_not_sensitive(self):
        """T33 附：统一社会信用代码是公开信息，不标 sensitive（区别于身份证）。"""
        import json as _j
        els = _j.loads((ROOT / "ontology/_shared/data_elements.json").read_text(encoding="utf-8"))["elements"]
        assert els["DE_CREDIT_CODE"].get("sensitive") is False
        assert els["DE_IDCARD"].get("sensitive") is True
