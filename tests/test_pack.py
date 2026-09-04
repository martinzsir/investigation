"""
tests/test_pack.py
REQ-044 多案件包与隔离测试。

覆盖 AC1–AC5：
  AC1: 案件包间默认隔离（跨包查询被拒）
  AC2: 新案件包可从模板初始化
  AC3: 包级 llm_policy 独立
  AC4: 切换案件包不影响其他包的版本与审计
  AC5: 跨包操作需显式授权并记录
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext
from core.pack import PackManager, PackIsolationError, PackNotFoundError


class TestREQ044PackIsolation(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        # 复制 default 包为测试基础
        src = ROOT / "ontology" / "default"
        dst = self.base / "default"
        import shutil
        shutil.copytree(src, dst)
        self.pm = PackManager(base_dir=self.base)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- AC1: 包间默认隔离 ----

    def test_ac1_cross_pack_denied(self):
        """AC1: 跨包操作默认被拒。"""
        ctx = AccessContext(operator="analyst", role="主办", case_id="default")
        # 同包放行
        self.pm.assert_authorized(ctx, "default")
        # 跨包拒绝
        with self.assertRaises(PackIsolationError):
            self.pm.assert_authorized(ctx, "case_002")

    def test_ac1_system_bypass(self):
        """AC1: system 角色旁路隔离。"""
        ctx = AccessContext(operator="admin", role="system", case_id="default")
        # system 可跨包
        self.pm.assert_authorized(ctx, "case_002")

    # ---- AC2: 从模板初始化 ----

    def test_ac2_init_from_template(self):
        """AC2: 新案件包可从模板初始化。"""
        self.pm.init_pack("case_2024_001", from_pack="default")
        self.assertTrue(self.pm.pack_exists("case_2024_001"))
        # objects.json 存在
        self.assertTrue((self.base / "case_2024_001" / "objects.json").exists())
        # bindings.json 存在；原结构化 source 被替换为占位 source_sql
        # （loader 视占位 source_sql 为合法声明，待新案件填入真实数据源）
        bindings = json.loads(
            (self.base / "case_2024_001" / "bindings.json").read_text("utf-8"))
        saw_placeholder = False
        for b in bindings.get("object_bindings", []):
            # 原 default 包中带结构化 source 的 binding 现在已无 source 键
            if "source_sql" in b and b["source_sql"].startswith("--"):
                self.assertNotIn("source", b)
                saw_placeholder = True
        self.assertTrue(saw_placeholder,
                        "init_pack 未写入占位 source_sql（REQ-044 AC2）")

    def test_ac2_init_duplicate_fails(self):
        """AC2: 初始化已存在的包名 → 失败。"""
        with self.assertRaises(ValueError):
            self.pm.init_pack("default", from_pack="default")

    def test_ac2_init_from_nonexistent_fails(self):
        """AC2: 从不存在的模板初始化 → 失败。"""
        with self.assertRaises(PackNotFoundError):
            self.pm.init_pack("new_case", from_pack="nonexistent")

    # ---- AC3: 包级 llm_policy 独立 ----

    def test_ac3_pack_level_llm_policy(self):
        """AC3: 各包 llm_policy.json 独立装载。"""
        self.pm.init_pack("case_a", from_pack="default")
        # 修改 case_a 的 llm_policy
        policy_path = self.base / "case_a" / "llm_policy.json"
        policy = json.loads(policy_path.read_text("utf-8"))
        policy["allowed_models"] = ["qwen-plus"]
        policy["network"] = "local"
        policy_path.write_text(json.dumps(policy, ensure_ascii=False), "utf-8")
        # default 的策略不受影响
        from core.llm.redact import load_llm_policy
        default_policy = load_llm_policy("default", base_dir=self.base)
        case_a_policy = load_llm_policy("case_a", base_dir=self.base)
        # 两个包的 allowed_models 不同
        self.assertEqual(case_a_policy["allowed_models"], ["qwen-plus"])
        self.assertEqual(default_policy["allowed_models"],
                         ["text-embedding-v3", "qwen-plus"])
        # case_a 改为 local，default 仍 isolated
        self.assertEqual(case_a_policy["network"], "local")
        self.assertEqual(default_policy["network"], "isolated")

    # ---- AC4: 切换包不影响其他包 ----

    def test_ac4_switch_pack_independent(self):
        """AC4: 切换案件包不影响其他包的版本与审计。"""
        self.pm.init_pack("case_b", from_pack="default")
        # 两个包的 ontology 声明各自独立
        pack_default = self.pm.load("default")
        pack_b = self.pm.load("case_b")
        # 对象声明相同（从模板复制）但实例不同
        self.assertEqual(
            [o.name for o in pack_default.objects],
            [o.name for o in pack_b.objects])
        # 修改 case_b 的 objects.json 不影响 default
        obj_path = self.base / "case_b" / "objects.json"
        data = json.loads(obj_path.read_text("utf-8"))
        # 添加一个新对象
        data["objects"].append({
            "name": "test_entity",
            "pk": "test_id",
            "kind": "entity",
            "name_property": "test_id",
            "properties": {"label": "enum"},
            "enum_values": {"label": ["a", "b"]},
        })
        obj_path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        # 同步给 case_b 的 bindings.json 加一条占位 binding
        bin_path = self.base / "case_b" / "bindings.json"
        bdata = json.loads(bin_path.read_text("utf-8"))
        bdata["object_bindings"].append({
            "object": "test_entity",
            "source_sql": "SELECT 't1' AS test_id, 'a' AS label",
        })
        bin_path.write_text(json.dumps(bdata, ensure_ascii=False), "utf-8")
        # default 不受影响
        pack_default_2 = self.pm.load("default")
        self.assertNotIn("test_entity", [o.name for o in pack_default_2.objects])
        pack_b_2 = self.pm.load("case_b")
        self.assertIn("test_entity", [o.name for o in pack_b_2.objects])

    # ---- AC5: 跨包授权 + 审计 ----

    def test_ac5_cross_pack_authorized(self):
        """AC5: 跨包操作需显式授权。"""
        ctx = AccessContext(operator="analyst", role="主办",
                            case_id="default+case_002")
        # 显式授权 case_002 → 放行
        self.pm.assert_authorized(ctx, "case_002")

    def test_ac5_cross_pack_audit(self):
        """AC5: 跨包操作记录审计。"""
        from core import Store
        s = Store(db_path=":memory:")
        c = s.conn
        # 建审计链表
        from core.audit import AuditChain
        chain = AuditChain(c)
        ctx = AccessContext(operator="analyst", role="human",
                            case_id="default+case_002")
        event_id = self.pm.cross_pack_audit(c, ctx, "case_002", "cross_read")
        self.assertIsNotNone(event_id)
        # 审计记录存在
        row = c.execute(
            "SELECT COUNT(*) FROM audit_chain WHERE event_id = ?",
            [event_id]).fetchone()
        self.assertEqual(row[0], 1)
        s.close()

    # ---- 辅助 ----

    def test_list_packs(self):
        """list_packs 返回所有已注册包。"""
        self.assertIn("default", self.pm.list_packs())
        self.pm.init_pack("case_c", from_pack="default")
        self.assertIn("case_c", self.pm.list_packs())

    def test_db_path_for_pack(self):
        """db_path_for_pack 生成正确的路径。"""
        from core.pack import db_path_for_pack
        path = db_path_for_pack("case_001", root="/tmp")
        self.assertTrue(path.endswith("investigation_case_001.duckdb"))


if __name__ == "__main__":
    unittest.main()
