"""
tests/test_runtime_context.py
R1: RuntimeContext + ReadOnlyStore（py 路径只读闭环）

验证用例：
  RT-TC-01: py 函数内 store.execute(...) → AttributeError
  RT-TC-02: py 函数执行 DROP TABLE → _assert_readonly 抛错
  RT-TC-03: ctx.table("nonexist") → ValueError
  RT-TC-06: ctx.table("call") → "obj_call"
  RT-TC-09: 旧签名函数（2 参数）仍可正常调用
"""
from __future__ import annotations

import unittest

from core import Store
from core.functions import FunctionExecutor, FUNCTION_IMPLS, register_function
from core.runtime_context import RuntimeContext, ReadOnlyStore
from core.ontology import build_ontology


def _new_sig_fn(store, params, ctx):
    """新签名 py 函数：使用 ctx.table() 派生表名。"""
    tbl = ctx.table("call")
    # 不实际查询业务表，只验证表名派生正确
    rows = store.query("SELECT 1 AS n")
    return {"hit": True, "table": tbl, "count": rows[0]["n"]}


def _old_sig_fn(store, params):
    """旧签名 py 函数（2 参数，向后兼容）。"""
    rows = store.query("SELECT 1 AS n")
    return {"hit": True, "count": rows[0]["n"]}


def _execute_attempt_fn(store, params, ctx):
    """尝试调用 store.execute —— 应被只读护栏拦截。"""
    store.execute("DROP TABLE obj_call")
    return {"hit": False}


def _drop_attempt_fn(store, params, ctx):
    """通过 query 执行 DROP —— 应被 _assert_readonly 拦截。"""
    store.query("DROP TABLE obj_call")
    return {"hit": False}


class RuntimeContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注册测试用 py 函数
        register_function("_rt_new_sig")(_new_sig_fn)
        register_function("_rt_old_sig")(_old_sig_fn)
        register_function("_rt_execute_attempt")(_execute_attempt_fn)
        register_function("_rt_drop_attempt")(_drop_attempt_fn)

    def _make_store(self):
        s = Store(db_path=":memory:")
        build_ontology(s.conn)
        return s

    def test_rt_tc_06_table_derivation(self):
        """RT-TC-06: ctx.table('call') 返回 'obj_call'。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            ctx = fx._make_ctx()
            self.assertEqual(ctx.table("call"), "obj_call")
            self.assertEqual(ctx.link("owns"), "lnk_owns")
        finally:
            s.close()

    def test_rt_tc_03_undeclared_table_raises(self):
        """RT-TC-03: ctx.table('nonexist') 抛 ValueError。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            ctx = fx._make_ctx()
            with self.assertRaises(ValueError):
                ctx.table("nonexist_obj_xyz")
            with self.assertRaises(ValueError):
                ctx.link("nonexist_link_xyz")
        finally:
            s.close()

    def test_rt_tc_01_store_executed_blocked(self):
        """RT-TC-01: py 函数内 store.execute 抛 AttributeError。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            # 直接测 ReadOnlyStore 屏蔽
            ro = fx._ro_store
            with self.assertRaises(AttributeError):
                ro.execute("SELECT 1")
        finally:
            s.close()

    def test_rt_tc_02_drop_query_blocked(self):
        """RT-TC-02: py 函数执行 DROP TABLE 被 _assert_readonly 拦截。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            ro = fx._ro_store
            with self.assertRaises(ValueError):
                ro.query("DROP TABLE obj_call")
        finally:
            s.close()

    def test_rt_tc_09_old_signature_works(self):
        """RT-TC-09: 旧签名函数（2 参数）仍可正常调用。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            result = fx._call_py("_rt_old_sig", {})
            self.assertTrue(result["hit"])
        finally:
            s.close()

    def test_rt_tc_new_signature_ctx_access(self):
        """新签名函数可通过 ctx.table() 访问表名。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            result = fx._call_py("_rt_new_sig", {})
            self.assertEqual(result["table"], "obj_call")
        finally:
            s.close()

    def test_rt_tc_execute_attempt_blocked_via_call(self):
        """通过 _call_py 调用尝试 execute 的函数应被拦截。"""
        s = self._make_store()
        try:
            fx = FunctionExecutor(s, "default")
            with self.assertRaises(AttributeError):
                fx._call_py("_rt_execute_attempt", {})
        finally:
            s.close()


class LoadPackCacheTests(unittest.TestCase):
    """R2: load_pack 缓存（指纹失效）。"""

    def test_rt_tc_05_cache_hit(self):
        """RT-TC-05: 多次 load_pack 返回同一对象（缓存命中）。"""
        from core.ontology_loader import load_pack, invalidate_pack_cache
        invalidate_pack_cache()
        p1 = load_pack("default")
        p2 = load_pack("default")
        self.assertIs(p1, p2, "未修改文件时应返回缓存对象")

    def test_rt_tc_05b_invalidate_works(self):
        """RT-TC-05b: invalidate_pack_cache 后重新解析。"""
        from core.ontology_loader import load_pack, invalidate_pack_cache
        invalidate_pack_cache()
        p1 = load_pack("default")
        invalidate_pack_cache("default")
        p2 = load_pack("default")
        self.assertIsNot(p1, p2, "invalidate 后应重新解析")


if __name__ == "__main__":
    unittest.main()
