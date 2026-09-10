# -*- coding: utf-8 -*-
"""Spike 前探 2：MCP 客户端配置/方法、MCPTool 包装、credential 字段、reply 用法。"""
import inspect


def section(title):
    print("\n" + "=" * 20, title, "=" * 20)


def show(obj, full=False):
    try:
        print(inspect.signature(obj.__init__))
    except Exception as e:
        print("sig err:", e)
    doc = inspect.getdoc(obj)
    if doc:
        print(doc[:1200])
    if full and hasattr(obj, "model_fields"):
        print("fields:", list(obj.model_fields.keys()))


from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig

section("StdioMCPConfig")
show(StdioMCPConfig, full=True)
section("HttpMCPConfig")
show(HttpMCPConfig, full=True)
section("MCPClient")
print("methods:", [m for m in dir(MCPClient) if not m.startswith("_")])
for name in ["__init__", "connect", "disconnect", "list_tools", "get_tool", "get_tools", "call_tool"]:
    if hasattr(MCPClient, name):
        fn = getattr(MCPClient, name)
        print(f"\n--- {name}", inspect.signature(fn) if callable(fn) else "")
        d = inspect.getdoc(fn)
        if d:
            print(d[:700])

section("MCPTool")
from agentscope.tool import MCPTool
print("methods:", [m for m in dir(MCPTool) if not m.startswith("_")])
show(MCPTool)

section("Credentials 字段")
from agentscope.credential import DashScopeCredential, OpenAICredential
print("dashscope fields:", list(DashScopeCredential.model_fields.keys()))
print("openai fields:", list(OpenAICredential.model_fields.keys()))

section("Agent.reply / Msg")
from agentscope.agent import Agent
print("reply:", inspect.signature(Agent.reply))
from agentscope.message import Msg
print("Msg fields:", list(Msg.model_fields.keys()) if hasattr(Msg, "model_fields") else dir(Msg))
