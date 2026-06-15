from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from .exceptions import LLMAPIError


class LLMClient:
    """Async OpenAI-compatible API client with streaming support."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-pro",
        max_tokens: int = 128000,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> dict:
        """Send a chat completion request. Returns the full response message dict.

        When streaming, on_token is called for each content delta.
        The returned dict always has the non-streaming format:
        {"role": "assistant", "content": "...", "tool_calls": [...]}
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        async with httpx.AsyncClient(timeout=120) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        raise LLMAPIError(response.status_code, text.decode()[:500])
                    return await self._parse_stream(response, on_token)
            except httpx.HTTPError as e:
                raise LLMAPIError(0, str(e))

    async def _parse_stream(
            self,
            response: httpx.Response,
            on_token: Callable[[str], None] | None,
    ) -> dict:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_call_buf: dict[int, dict] = {}

        import httpx  # 确保能捕获 httpx 异常
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break

                import json
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices")
                if not choices:
                    continue

                choice = choices[0] if isinstance(choices, list) else choices
                delta = choice.get("delta", {})

                # Reasoning content delta (DeepSeek thinking mode)
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    reasoning_parts.append(delta["reasoning_content"])

                # Content delta
                if "content" in delta and delta["content"]:
                    token = delta["content"]
                    content_parts.append(token)
                    if on_token:
                        on_token(token)

                # Tool call delta
                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_call_buf:
                            tool_call_buf[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                        if "id" in tc and tc["id"]:
                            tool_call_buf[idx]["id"] = tc["id"]
                        if "function" in tc:
                            if "name" in tc["function"] and tc["function"]["name"]:
                                tool_call_buf[idx]["function"]["name"] = tc["function"]["name"]
                            if "arguments" in tc["function"]:
                                tool_call_buf[idx]["function"]["arguments"] += tc["function"]["arguments"]
        except httpx.HTTPError:
            # 极简防御：如果服务端异常掐断了流（如 RemoteProtocolError 或 Timeout）
            # 我们优雅地吞掉异常，退出循环，直接保留并返回已经收集到的半截数据。
            pass

        # Build tool_calls list from buffer
        for idx in sorted(tool_call_buf.keys()):
            tc = tool_call_buf[idx]
            tc["type"] = "function"
            tool_calls.append(tc)

        result: dict = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if reasoning_parts:
            result["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            result["tool_calls"] = tool_calls

        return result
