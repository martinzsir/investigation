"""
tests/test_llm_policy.py
REQ-038 LLM 策略与脱敏闸门：
  - AC1：network=isolated 时 call_llm 拒绝并落 blocked 日志（审计可查）
  - AC2：身份证/手机号/银行卡/精确轨迹/通话正文/真名不出网（脱敏复扫零命中）
  - AC3：build_redacted_context 只给 rule_id/级别/维度/间类/计数/字段名/URI，无原始明细
  - AC4：llm_call_log 字段齐全 + AuditChain 哈希链可校验（允许/拒绝两类记录）
  - AC5：策略文件缺失/network 缺省 → fail-closed（isolated + 空模型白名单）
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core import Store
from core.access import AccessContext, LLMBlockedError
from core.audit import AuditChain
from core.llm.redact import (
    build_redacted_context,
    call_llm,
    load_llm_policy,
    log_llm_call,
    redact_payload,
    redact_text,
    scan_pii,
    tokenize_name,
)

PII_PATTERNS = [
    re.compile(r"\d{17}[\dXx]"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
]


def _pii_hits(text: str) -> list[str]:
    return [m.group(0) for p in PII_PATTERNS for m in p.finditer(str(text))]


def _findings_with_pii() -> list[dict]:
    """构造含真实 PII 形态与敏感明细的 findings（模拟脱敏前内部数据）。"""
    return [
        {
            "rule_id": "R1",
            "候选虚处": "季末整数存入",
            "级别": "待核实",
            "dimension": "资金",
            "jian_types": ["生间"],
            "source_rows": [
                {"q": "2021Q3", "cnt": 3, "amt": 300000.0,
                 "raw_name": "张卫国", "身份证号": "310101199001011234",
                 "手机号": "13812345678", "银行卡号": "6222021234567890123",
                 "通话内容": "明天把钱转到指定账户",
                 "轨迹": "2021-09-30 10:00 某银行网点",
                 "备注": "经办人手机号 13998765432"},
            ],
            "is_degraded": False,
        },
    ]


class LlmPolicyTests(unittest.TestCase):

    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.conn = self.store.conn

    def tearDown(self):
        self.store.close()

    # ---- AC5：fail-closed 策略装载 ----
    def test_ac5_fail_closed_when_policy_missing_or_invalid(self):
        """AC5：文件缺失 / network 缺省 / allowed_models 缺失 → 一律 isolated + 空白名单。"""
        # 文件缺失：不存在的 pack
        p = load_llm_policy("__no_such_pack__")
        self.assertEqual(p["network"], "isolated")
        self.assertEqual(p["allowed_models"], [])
        self.assertEqual(p["fallback"], "deterministic_only")
        # 缺 network 字段
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "badpack"
            root.mkdir()
            (root / "llm_policy.json").write_text(
                json.dumps({"schema_version": 2, "allowed_models": ["gpt-x"]}),
                encoding="utf-8")
            p2 = load_llm_policy("badpack", base_dir=Path(td))
            self.assertEqual(p2["network"], "isolated",
                             "缺 network 声明必须 fail-closed")
            self.assertEqual(p2["allowed_models"], [],
                             "fail-closed 时模型白名单必须清空")
            # JSON 非法
            (root / "llm_policy.json").write_text("{ not json", encoding="utf-8")
            p3 = load_llm_policy("badpack", base_dir=Path(td))
            self.assertEqual(p3["network"], "isolated")
        # 默认包策略本身就是 isolated（内核环境声明）；allowed_models 允许
        # 声明候选模型（如语义检索 embedding / 草案器），isolated 拒网下不生效
        default_p = load_llm_policy("default")
        self.assertEqual(default_p["network"], "isolated")
        self.assertIsInstance(default_p["allowed_models"], list)

    # ---- AC2：脱敏 ----
    def test_ac2_pii_redacted_before_egress(self):
        """AC2：身份证/手机号/银行卡遮蔽，轨迹/通话正文整段丢弃，人名 tokenize。"""
        policy = load_llm_policy("default")
        # 文本遮蔽
        text = "身份证 310101199001011234，手机 13812345678，卡号 6222021234567890123"
        red, report = redact_text(text, policy)
        self.assertEqual(_pii_hits(red), [], f"遮蔽后仍检出 PII：{red}")
        self.assertIn("310", red)       # 保留前 3
        self.assertIn("1234", red)      # 保留后 4
        self.assertEqual(report["counts"]["id_card"], 1)
        self.assertEqual(report["counts"]["phone"], 1)
        self.assertEqual(report["counts"]["bank_card"], 1)
        self.assertTrue(report["redaction_hash"])
        # 结构化载荷
        payload = {
            "raw_name": "张卫国",
            "caller_raw": "李志强",
            "轨迹": "2021-09-30 某网点",
            "通话内容": "请尽快转账",
            "rows": [{"手机号": "联系 13812345678", "amt": 100000}],
        }
        out = redact_payload(payload, policy)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertEqual(_pii_hits(blob), [], f"载荷脱敏后仍检出 PII：{blob}")
        self.assertNotIn("张卫国", blob)
        self.assertNotIn("李志强", blob)
        self.assertTrue(out["raw_name"].startswith("当事人#"))
        # 同名同 token（关联分析能力保留）
        self.assertEqual(tokenize_name("张卫国"), out["raw_name"])
        self.assertEqual(out["轨迹"], "[REDACTED:precise_track]")
        self.assertEqual(out["通话内容"], "[REDACTED:call_content]")
        # 脱敏上下文复扫
        ctx = build_redacted_context(_findings_with_pii(), policy)
        ctx_blob = json.dumps(
            {k: v for k, v in ctx.items() if k not in ("redaction_hash", "pii_rescan")},
            ensure_ascii=False)
        self.assertEqual(_pii_hits(ctx_blob), [],
                         f"脱敏上下文复扫检出 PII：{ctx_blob[:300]}")
        self.assertTrue(ctx["pii_rescan"]["clean"])

    # ---- AC3：上下文只有 token/类型/计数 ----
    def test_ac3_context_has_only_metadata(self):
        """AC3：上下文中不得出现原始明细值（金额/真名/正文/地点）。"""
        ctx = build_redacted_context(_findings_with_pii())
        blob = json.dumps(ctx, ensure_ascii=False)
        for leaked in ("张卫国", "300000", "某银行网点", "明天把钱",
                       "310101199001011234", "13812345678", "6222021234567890123",
                       "经办人"):
            self.assertNotIn(leaked, blob, f"AC3 失败：原始明细泄漏 {leaked!r}")
        item = ctx["findings"][0]
        # 只允许元数据键
        self.assertEqual(
            set(item),
            {"rule_id", "级别", "dimension", "jian_types", "source_row_count",
             "source_row_fields", "evidence_uris", "is_degraded"})
        self.assertEqual(item["rule_id"], "R1")
        self.assertEqual(item["级别"], "待核实")
        self.assertEqual(item["source_row_count"], 1)
        # 字段名清单保留（类型信息），值不保留
        self.assertIn("raw_name", item["source_row_fields"])
        self.assertIn("amt", item["source_row_fields"])
        self.assertTrue(ctx["redaction_hash"])

    # ---- AC1：isolated 拒绝并落 blocked 日志 ----
    def test_ac1_isolated_blocked_and_logged(self):
        """AC1：isolated 上下文调 LLM 被拒，llm_call_log 落 blocked 记录。"""
        policy = load_llm_policy("default")
        ctx = AccessContext(operator="王检察官", role="主办",
                            network="isolated", purpose="线索复查")
        redacted = build_redacted_context(_findings_with_pii(), policy)
        with self.assertRaises(LLMBlockedError) as cm:
            call_llm(self.conn, ctx, policy, model="fake-model-1",
                     prompt="请复查", redacted_input=redacted,
                     fake_invoke=lambda **kw: {"x": 1})
        self.assertIn("isolated", str(cm.exception))
        rows = self.conn.execute(
            "SELECT allowed, blocked_reason, operator, model FROM llm_call_log"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        allowed, reason, operator, model = rows[0]
        self.assertFalse(allowed)
        self.assertIn("isolated", reason)
        self.assertEqual(operator, "王检察官")
        self.assertEqual(model, "fake-model-1")

    # ---- AC4：日志字段 + 审计链（含放行路径）----
    def test_ac4_log_fields_and_audit_chain(self):
        """AC4：llm_call_log 字段齐全；放行/拒绝两类记录均挂 AuditChain 且链可校验。"""
        policy = dict(load_llm_policy("default"))
        policy["allowed_models"] = ["fake-model-1"]   # 测试放行模型
        ctx_ok = AccessContext(operator="李主办", role="主办",
                               network="local", purpose="对齐复核")
        redacted = build_redacted_context(_findings_with_pii(), policy)
        invoked = []

        def fake(**kw):
            invoked.append(kw["model"])
            return {"summary": "确定性占位回复", "needs_human_review": True}

        out = call_llm(self.conn, ctx_ok, policy, model="fake-model-1",
                       prompt="请对齐", redacted_input=redacted,
                       fake_invoke=fake)
        self.assertTrue(out["ok"])
        self.assertEqual(invoked, ["fake-model-1"])

        # 模型白名单拦截（local 网络但模型不在白名单）
        ctx_bad = AccessContext(operator="李主办", role="主办", network="local")
        with self.assertRaises(LLMBlockedError):
            call_llm(self.conn, ctx_bad, policy, model="gpt-evil",
                     prompt="x", redacted_input=redacted,
                     fake_invoke=lambda **kw: None)

        rows = self.conn.execute(
            """SELECT log_id, operator, network, model, allowed, prompt_hash,
                      input_redaction_hash, tool_calls, blocked_reason, audit_event_id
               FROM llm_call_log ORDER BY occurred_at""").fetchall()
        self.assertEqual(len(rows), 2)
        ok_row = next(r for r in rows if r[4])
        bad_row = next(r for r in rows if not r[4])
        for r in rows:
            log_id, operator, network, model, allowed, prompt_hash, irh, tc, reason, evid = r
            self.assertTrue(log_id.startswith("llmlog_"))
            self.assertTrue(operator)
            self.assertIn(network, ("local", "isolated"))
            self.assertTrue(prompt_hash and len(prompt_hash) == 64)
            if allowed:
                self.assertTrue(irh and len(irh) == 64)
                self.assertIsNone(reason)
            else:
                self.assertTrue(reason)
            self.assertEqual(json.loads(tc), [])
            self.assertTrue(evid, "blocked/allowed 记录都必须挂 audit_event_id")
        self.assertIn("白名单", bad_row[8])
        # 审计链可校验且含两条事件
        chain = AuditChain(self.conn)
        self.assertTrue(chain.chain_verify())
        self.assertGreaterEqual(chain.count(), 2)
        ev = self.conn.execute(
            "SELECT after_state FROM audit_chain WHERE event_id = ?",
            [ok_row[9]]).fetchone()
        after = json.loads(ev[0])
        self.assertEqual(after["action"], "llm_call")
        self.assertTrue(after["allowed"])
        # 生产无 fake_invoke → 确定性回退（也落 blocked 日志）
        with self.assertRaises(LLMBlockedError) as cm:
            call_llm(self.conn, ctx_ok, policy, model="fake-model-1",
                     prompt="请对齐", redacted_input=redacted)
        self.assertIn("deterministic_only", str(cm.exception))

    def test_redaction_hash_tampering_rejected(self):
        """补充：redacted_input 缺 redaction_hash → 闸门拒绝（禁止未脱敏输入出网）。"""
        policy = dict(load_llm_policy("default"))
        policy["allowed_models"] = ["fake-model-1"]
        ctx = AccessContext(operator="李主办", role="主办", network="local")
        with self.assertRaises(LLMBlockedError) as cm:
            call_llm(self.conn, ctx, policy, model="fake-model-1",
                     prompt="x", redacted_input={"findings": []},
                     fake_invoke=lambda **kw: None)
        self.assertIn("redaction_hash", str(cm.exception))
        # PII 复扫命中 → 拒绝
        bad = {"redaction_hash": "0" * 64, "note": "手机 13812345678"}
        with self.assertRaises(LLMBlockedError) as cm:
            call_llm(self.conn, ctx, policy, model="fake-model-1",
                     prompt="x", redacted_input=bad,
                     fake_invoke=lambda **kw: None)
        self.assertIn("PII", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
