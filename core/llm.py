from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

try:
    from deepseek_tokenizer import ds_token
except ImportError:  # pragma: no cover
    ds_token = None

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
        self._tokenizer = ds_token

    def count_tokens(self, text: str) -> int:
        """Count tokens in a single string using DeepSeek's tokenizer.
        Falls back to character heuristic if tokenizer unavailable.
        """
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text))
        # Fallback heuristic
        return max(1, len(text.encode("utf-8", errors="ignore")) // 3)

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """Count tokens across a full messages array.
        Includes per-message format overhead (~4 tokens per message).
        """
        total = 0
        for msg in messages:
            total += 4  # role + formatting overhead
            for key, value in msg.items():
                if isinstance(value, str):
                    total += self.count_tokens(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            total += self.count_tokens(str(item))
        return max(1, total)

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

        timeout_config = httpx.Timeout(600.0)
        max_retries = 3

        async with httpx.AsyncClient(timeout=timeout_config) as client:
            for attempt in range(max_retries):
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
                    if attempt == max_retries - 1:
                        error_detail = f"{type(e).__name__}: {str(e) or 'Connection dropped or timed out'}"
                        raise LLMAPIError(0, error_detail)
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s

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

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logging.getLogger(__name__).debug("Failed to parse SSE data line: %s", data[:200])
                    continue

                choices = chunk.get("choices")
                if not choices:
                    continue

                choice = choices[0] if isinstance(choices, list) else choices
                delta = choice.get("delta", {})

                # Reasoning content delta (DeepSeek thinking mode)
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    token = delta["reasoning_content"]
                    reasoning_parts.append(token)
                    if on_token:
                        if not reasoning_started:
                            on_token("> 🧠 **Thinking...**\n> ")
                            reasoning_started = True
                        on_token(token.replace("\n", "\n> "))

                # Content delta
                if "content" in delta and delta["content"]:
                    token = delta["content"]
                    content_parts.append(token)
                    if on_token:
                        if reasoning_started and not content_started:
                            on_token("\n\n")
                        content_started = True
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
        except httpx.HTTPError as e:
                                # 极简防御：如果服务端异常掐断了流
             if not content_parts and not tool_call_buf:
                                    # 如果什么数据都没拿到就断开了（比如彻底超时），必须抛出异常让 UI 知道
                     raise LLMAPIError(0, f"Stream connection dropped or timed out: {e}")
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
