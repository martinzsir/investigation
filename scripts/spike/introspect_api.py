# -*- coding: utf-8 -*-
"""Spike 前探：AgentScope 2.0.8 的 MCP 集成入口与模型/Agent 调用签名（只读探测，不连网）。"""
import inspect
import pkgutil
import importlib


def section(title):
    print("\n" + "=" * 20, title, "=" * 20)


section("agentscope 顶层子模块")
import agentscope
for m in pkgutil.iter_modules(agentscope.__path__):
    print(" ", m.name)

section("agentscope.tool 模块导出（含 mcp 字样）")
import agentscope.tool as t
print([x for x in dir(t) if not x.startswith("_")])

section("尝试导入 agentscope.mcp")
try:
    import agentscope.mcp as mcp
    print("agentscope.mcp OK:", [x for x in dir(mcp) if not x.startswith("_")])
except Exception as e:
    print("no agentscope.mcp:", type(e).__name__, e)

section("全仓搜索 MCP 相关类")
found = []
for modinfo in pkgutil.walk_packages(agentscope.__path__, prefix="agentscope."):
    name = modinfo.name
    if "mcp" in name.lower():
        found.append(name)
print(found)

section("Toolkit.add_tool 签名")
from agentscope.tool import Toolkit
print(inspect.signature(Toolkit.add_tool))
print(inspect.getdoc(Toolkit.add_tool)[:800] if inspect.getdoc(Toolkit.add_tool) else "(no doc)")

section("DashScopeChatModel 构造签名")
from agentscope.model import DashScopeChatModel, OpenAIChatModel
from agentscope.credential import DashScopeCredential, OpenAICredential
print("DashScopeChatModel:", inspect.signature(DashScopeChatModel.__init__))
print("DashScopeCredential:", inspect.signature(DashScopeCredential.__init__))
print("OpenAIChatModel:", inspect.signature(OpenAIChatModel.__init__))
print("OpenAICredential:", inspect.signature(OpenAICredential.__init__))

section("Agent 调用方式")
from agentscope.agent import Agent
print("call:", inspect.signature(Agent.__call__))
print("reply:", [m for m in dir(Agent) if not m.startswith("_")])
