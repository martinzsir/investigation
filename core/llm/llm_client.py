"""
core/llm/llm_client.py
Qwen3 在线 API 封装——LLM 调用的统一出口。

设计：
  - 生产调用 Qwen3 API（DashScope 兼容接口 / OpenAI 兼容接口）；
  - 通过环境变量 DASHSCOPE_API_KEY 获取密钥；
  - 支持 fake_invoke 注入（测试/离线模式），与 core/llm/redact.py call_llm 闸门兼容；
  - 返回统一格式 {ok, model, result}。

使用方式：
  from core.llm.llm_client import LLMClient
  client = LLMClient(model="qwen-plus", api_key="sk-xxx")
  resp = client.chat(messages=[{"role":"user","content":"..."}])
  # resp = {"ok": True, "model": "qwen-plus", "result": {"content": "...", ...}}
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any, Callable


# Qwen3 默认模型名（可被构造参数覆盖）
DEFAULT_MODEL = "qwen-plus"
# DashScope OpenAI 兼容接口
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class LLMClient:
    """Qwen3 API 封装客户端。

    优先级：
      1. 构造参数 api_key / model
      2. 环境变量 DASHSCOPE_API_KEY
      3. 无 key → 无法调用（返回 ok=False）

    fake_invoke 注入时跳过网络调用，直接返回注入函数的结果。
    """

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, fake_invoke: Callable | None = None):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or DEFAULT_BASE_URL
        self._fake_invoke = fake_invoke

    def chat(self, messages: list[dict], temperature: float = 0.3,
             max_tokens: int = 2048, **kwargs) -> dict[str, Any]:
        """调用 chat completions 接口。

        Args:
            messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
            temperature: 采样温度（确定性优先用低值）
            max_tokens: 最大生成 token 数

        Returns:
            {"ok": True, "model": ..., "result": {"content": "...", "raw": {...}}}
            或 {"ok": False, "model": ..., "error": "..."}
        """
        # 注入模式（测试/离线）
        if self._fake_invoke is not None:
            result = self._fake_invoke(
                model=self.model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            return {"ok": True, "model": self.model, "result": result}

        if not self.api_key:
            return {"ok": False, "model": self.model,
                    "error": "DASHSCOPE_API_KEY 未设置"}

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        payload.update(kwargs)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "model": self.model,
                    "result": {"content": content, "raw": body}}
        except urllib.error.HTTPError as e:
            err_msg = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}"
            return {"ok": False, "model": self.model, "error": err_msg}
        except Exception as e:
            return {"ok": False, "model": self.model, "error": str(e)}

    def chat_json(self, messages: list[dict], **kwargs) -> dict[str, Any]:
        """调用 chat 并解析 JSON 结果。

        在 system message 中追加"以 JSON 格式输出"指令；
        尝试从 content 中提取 JSON（支持 ```json ... ``` 包裹）。
        """
        resp = self.chat(messages, **kwargs)
        if not resp.get("ok"):
            return resp
        content = resp["result"]["content"]
        # 尝试提取 JSON
        json_str = content
        if "```json" in json_str:
            start = json_str.index("```json") + 7
            end = json_str.rfind("```")
            json_str = json_str[start:end].strip()
        elif "```" in json_str:
            start = json_str.index("```") + 3
            end = json_str.rfind("```")
            json_str = json_str[start:end].strip()
        try:
            parsed = json.loads(json_str)
            resp["result"]["parsed"] = parsed
        except (json.JSONDecodeError, ValueError):
            resp["result"]["parsed"] = None
        return resp
