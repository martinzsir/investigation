"""
server/app/worker/escape_hatch.py
W-031：代码逃生舱——四类扩展代码桩生成。

四类扩展（必须写代码，不做租户沙箱在线执行）：
  1. function：新 py Function（FUNCTION_IMPLS 注册 + functions.json 声明）
  2. value_type：新值类型（TYPE_SQL 字典追加）
  3. clean_rule：新清洗规则（clean 函数 + bindings.json clean 字段）
  4. side_effect：新 Action 副作用（ALLOWED_SIDE_EFFECTS + action_executor）

生成内容：函数签名 + 输入输出契约 + 测试骨架（初始失败）+ 注册点说明。
不写文件、不注册、不执行——仅返回代码桩文本，由开发者手动合入。
"""
from __future__ import annotations

from typing import Any

EXT_TYPES = ("function", "value_type", "clean_rule", "side_effect")


def generate_stub(ext_type: str, name: str,
                  description: str = "") -> dict[str, Any]:
    """生成指定类型的代码桩。

    Returns:
        {files: [{path, content}], registration_points: [str]}
    """
    if ext_type not in EXT_TYPES:
        raise ValueError(
            f"不支持的扩展类型：{ext_type}（可用：{EXT_TYPES}）")
    if not name or not name.strip():
        raise ValueError("name 不能为空")

    generators = {
        "function": _gen_function,
        "value_type": _gen_value_type,
        "clean_rule": _gen_clean_rule,
        "side_effect": _gen_side_effect,
    }
    return generators[ext_type](name.strip(), description.strip())


def _gen_function(name: str, desc: str) -> dict[str, Any]:
    """py Function 代码桩。"""
    impl = f'''"""
core/functions/{name}.py
逃生舱生成的 py Function 代码桩。

扩展需求：{desc}

注册点：
  1. 本文件实现函数后，在 core/functions.py 底部追加：
       from core.functions.{name} import {name} as _{name}
       register_function("{name}")(_{name})
  2. 在 ontology/<pack>/functions.json 声明：
       {{"name": "{name}", "impl_kind": "py", "impl_ref": "{name}",
         "output_type": "rows", "parameters": {{}}}}
"""
from __future__ import annotations

from typing import Any


def {name}(store, params: dict) -> dict:
    """{desc}

    输入契约：
      - store：只读连接封装（store.query(sql) -> list[dict]）
      - params：functions.json 声明的 parameters（已校验类型）

    输出契约：
      - 返回 dict，含 rows（list[dict]）或自定义结构
      - 可自报降级：返回 {{"degraded": True, "degraded_reason": "..."}}
    """
    # TODO: 实现检测逻辑
    # rows = store.query("SELECT ...")
    raise NotImplementedError("{name} 待实现")
'''
    test = f'''"""
tests/test_{name}_function.py
逃生舱生成的测试骨架（初始为失败状态，AC-3）。
"""
from __future__ import annotations

import unittest


class {name.title()}FunctionTest(unittest.TestCase):
    def test_{name}_returns_rows(self):
        from core.functions.{name} import {name}
        # TODO: 构造 store mock，验证返回结构
        self.fail("TODO: 实现 {name} 测试")


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "files": [
            {"path": f"core/functions/{name}.py", "content": impl},
            {"path": f"tests/test_{name}_function.py", "content": test},
        ],
        "registration_points": [
            "core/functions.py: FUNCTION_IMPLS（通过 @register_function 装饰器）",
            "ontology/<pack>/functions.json: 声明 name/impl_kind/impl_ref/output_type/parameters",
        ],
    }


def _gen_value_type(name: str, desc: str) -> dict[str, Any]:
    """值类型代码桩。"""
    impl = f'''"""
值类型扩展：{name}

扩展需求：{desc}

注册点：
  core/ontology.py: TYPE_SQL 字典追加：
      TYPE_SQL["{name}"] = "<DuckDB SQL 类型>"

  同时需在 ontology_loader.py 的值类型校验白名单中确认支持。
"""
# 在 core/ontology.py 的 TYPE_SQL 字典中追加：
# TYPE_SQL = {{
#     ...
#     "{name}": "<DuckDB 列类型，如 VARCHAR/BIGINT/...>",
# }}
'''
    test = f'''"""
tests/test_{name}_valuetype.py
逃生舱生成的测试骨架（初始为失败状态，AC-3）。
"""
from __future__ import annotations

import unittest


class {name.title()}ValueTypeTest(unittest.TestCase):
    def test_type_registered(self):
        from core.ontology import TYPE_SQL
        self.assertIn("{name}", TYPE_SQL)
        self.fail("TODO: 实现 {name} 值类型物化测试")


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "files": [
            {"path": f"core/valuetype_{name}.py.stub", "content": impl},
            {"path": f"tests/test_{name}_valuetype.py", "content": test},
        ],
        "registration_points": [
            "core/ontology.py: TYPE_SQL 字典",
        ],
    }


def _gen_clean_rule(name: str, desc: str) -> dict[str, Any]:
    """清洗规则代码桩。"""
    impl = f'''"""
core/functions/{name}_clean.py
逃生舱生成的清洗规则代码桩。

扩展需求：{desc}

注册点：
  1. 在 ontology/<pack>/bindings.json 的 object_bindings[].clean 字段引用：
       "clean": "{name}"
  2. 清洗函数与 Function 同机制注册（FUNCTION_IMPLS）。
"""
from __future__ import annotations


def {name}(value: str) -> str:
    """{desc}

    输入契约：原始字符串值
    输出契约：清洗后字符串值；无法清洗返回原值或 None
    """
    # TODO: 实现清洗逻辑
    raise NotImplementedError("{name} 清洗规则待实现")
'''
    test = f'''"""
tests/test_{name}_clean.py
逃生舱生成的测试骨架（初始为失败状态，AC-3）。
"""
from __future__ import annotations

import unittest


class {name.title()}CleanTest(unittest.TestCase):
    def test_clean(self):
        from core.functions.{name}_clean import {name}
        # self.assertEqual({name}(" 原始值 "), "清洗后")
        self.fail("TODO: 实现 {name} 清洗测试")


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "files": [
            {"path": f"core/functions/{name}_clean.py", "content": impl},
            {"path": f"tests/test_{name}_clean.py", "content": test},
        ],
        "registration_points": [
            "core/functions.py: FUNCTION_IMPLS（通过 @register_function）",
            "ontology/<pack>/bindings.json: object_bindings[].clean 字段",
        ],
    }


def _gen_side_effect(name: str, desc: str) -> dict[str, Any]:
    """Action 副作用代码桩。"""
    impl = f'''"""
Action 副作用扩展：{name}

扩展需求：{desc}

注册点（三步）：
  1. core/ontology_loader.py: ALLOWED_SIDE_EFFECTS 追加 "{name}"
  2. ontology/<pack>/actions.json: 声明动作，side_effects 含 "{name}"
  3. core/action_executor.py: _execute_side_effect 中处理 "{name}" 分支
"""
# 1. core/ontology_loader.py:
# ALLOWED_SIDE_EFFECTS = {{
#     "set_clue_status", "create_decision",
#     "merge_entity", "dismiss_review",
#     "{name}",  # 新增
# }}

# 2. ontology/<pack>/actions.json:
# {{
#   "actions": [
#     {{
#       "name": "<action_name>",
#       "side_effects": ["{name}"],
#       "roles": ["正兵", "human"],
#       "params": {{}}
#     }}
#   ]
# }}

# 3. core/action_executor.py _execute_side_effect:
# elif side_effect == "{name}":
#     # TODO: 实现 {name} 副作用
#     raise NotImplementedError("{name} 待实现")
'''
    test = f'''"""
tests/test_{name}_sideeffect.py
逃生舱生成的测试骨架（初始为失败状态，AC-3）。
"""
from __future__ import annotations

import unittest


class {name.title()}SideEffectTest(unittest.TestCase):
    def test_side_effect_registered(self):
        from core.ontology_loader import ALLOWED_SIDE_EFFECTS
        self.assertIn("{name}", ALLOWED_SIDE_EFFECTS)
        self.fail("TODO: 实现 {name} 副作用执行测试")


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "files": [
            {"path": f"core/sideeffect_{name}.py.stub", "content": impl},
            {"path": f"tests/test_{name}_sideeffect.py", "content": test},
        ],
        "registration_points": [
            "core/ontology_loader.py: ALLOWED_SIDE_EFFECTS 集合",
            "ontology/<pack>/actions.json: 动作声明 side_effects 字段",
            "core/action_executor.py: _execute_side_effect 分支处理",
        ],
    }
