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
import re
import urllib.request
import urllib.error
from typing import Any, Callable


# 代码块围栏（语言标记大小写不一/可能缺省，如 ```JSON、``` json）
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n?(.*?)```",
                       re.DOTALL)


def extract_json(content: str) -> Any | None:
    """从模型文本回复中尽力提取 JSON 对象/数组。

    三级尝试，全部失败返回 None（调用方可据此做格式纠偏重问）：
      1. 整段直接 json.loads（裸 JSON）；
      2. 逐个 ```围栏``` 代码块（语言标记大小写不敏感、可缺省）；
      3. 花括号/方括号配平扫描（跳过字符串内符号与转义），
         覆盖“散文 + JSON”且未围栏的回复。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    # 1) 整段
    try:
        return json.loads(content.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) 围栏代码块（取第一个能解析成功的块）
    for m in _FENCE_RE.finditer(content):
        body = m.group(2).strip()
        if not body:
            continue
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
    # 3) 配平扫描：从首个 { 或 [ 出发，按字符串感知匹配收尾符号
    start = min(
        (i for i in (content.find("{"), content.find("[")) if i >= 0),
        default=-1)
    if start >= 0:
        pairs = {"{": "}", "[": "]"}
        stack: list[str] = []
        in_str = False
        esc = False
        for i in range(start, len(content)):
            ch = content[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in pairs:
                stack.append(pairs[ch])
            elif ch in ("}", "]"):
                if not stack or ch != stack[-1]:
                    break  # 结构已坏，无需继续
                stack.pop()
                if not stack:
                    try:
                        return json.loads(content[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
    return None


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

    def chat_stream(self, messages: list[dict], temperature: float = 0.3,
                    max_tokens: int = 2048, **kwargs):
        """流式调用 chat completions，逐 token 生成。

        生成器产出 {"delta": "text"} 块；出错产出 {"error": "..."}。
        使用 SSE 协议读取 DashScope OpenAI 兼容接口的流式响应。
        """
        if not self.api_key:
            yield {"error": "DASHSCOPE_API_KEY 未设置"}
            return

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        payload.update(kwargs)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=90)
            with resp:
                for line in resp:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = (chunk.get("choices", [{}])[0]
                                 .get("delta", {}).get("content", ""))
                        if delta:
                            yield {"delta": delta}
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
        except Exception as e:
            yield {"error": str(e)}

    # 首次解析失败后的格式纠偏指令（只针对非/坏 JSON 重问一次）
    _JSON_REPAIR_HINT = (
        "上一次输出无法被 JSON 解析器解析。请严格只输出一个符合前述结构的 "
        "JSON 对象：不要代码块围栏（```）、不要任何解释或前后缀文字、"
        "不要省略 JSON 闭合括号。")

    def chat_json(self, messages: list[dict], *, repair_retry: bool = True,
                  **kwargs) -> dict[str, Any]:
        """调用 chat 并解析 JSON 结果。

        - 提取经 extract_json()：兼容裸 JSON、大小写/缺省语言标记的
          ```围栏```、散文包裹的 JSON；
        - repair_retry=True 时，首次解析失败只做一次格式纠偏重问
          （模型偶发跑偏不直接打回用户）；
        - 最终仍失败时 result.parsed=None，并附 parse_error（长度+片段）
          供上层诊断（不吞掉真实回包）。
        """
        resp = self.chat(messages, **kwargs)
        if not resp.get("ok"):
            return resp
        content = resp["result"].get("content", "")
        parsed = extract_json(content)
        if parsed is None and repair_retry:
            retry_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": self._JSON_REPAIR_HINT},
            ]
            retry_resp = self.chat(retry_messages, **kwargs)
            if retry_resp.get("ok"):
                content = retry_resp["result"].get("content", "")
                parsed = extract_json(content)
                resp["result"]["raw_retry"] = retry_resp["result"].get("raw")
        result = resp["result"]
        result["parsed"] = parsed
        if parsed is None:
            preview = str(content or "")[:200].replace("\n", " ")
            result["parse_error"] = (
                f"模型回包无法提取 JSON（{len(str(content or ''))} 字符，"
                f"片段：{preview}）")
        return resp
