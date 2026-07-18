from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from .model_names import DEFAULT_MODEL, normalize_model_name

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
        model: str = DEFAULT_MODEL,
        max_tokens: int = 128000,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = normalize_model_name(model)
        self.max_tokens = max_tokens
        self._tokenizer = ds_token

    def count_tokens(self, text: str) -> int:
        """Count tokens in a single string using the configured tokenizer."""
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text))
        return max(1, len(text.encode("utf-8", errors="ignore")) // 3)

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """Count tokens across a full messages array with small format overhead."""
        total = 0
        for msg in messages:
            total += 4
            for value in msg.values():
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
        """Send a streaming chat completion request and return one message dict."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        timeout_config = httpx.Timeout(600.0)
        max_retries_network = 3
        max_retries_server = 4
        max_retries_rate = 5

        async with httpx.AsyncClient(timeout=timeout_config) as client:
            for attempt in range(max(max_retries_network, max_retries_server, max_retries_rate)):
                try:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=body,
                    ) as response:
                        if response.status_code == 200:
                            result = await self._parse_stream(response, on_token)
                            provider_usage = result.get("_provider_usage")
                            if isinstance(provider_usage, dict):
                                result["_usage"] = {
                                    "prompt_tokens": int(provider_usage.get("prompt_tokens", 0) or 0),
                                    "completion_tokens": int(provider_usage.get("completion_tokens", 0) or 0),
                                    "estimated": False,
                                }
                            else:
                                completion_payload = "".join([
                                    str(result.get("reasoning_content") or ""),
                                    str(result.get("content") or ""),
                                    json.dumps(result.get("tool_calls") or [], ensure_ascii=False),
                                ])
                                result["_usage"] = {
                                    "prompt_tokens": self.count_messages_tokens(messages),
                                    "completion_tokens": self.count_tokens(completion_payload),
                                    "estimated": True,
                                }
                            return result

                        text = await response.aread()
                        error_body = text.decode()[:500]

                        if response.status_code == 429:
                            if attempt >= max_retries_rate - 1:
                                raise LLMAPIError(response.status_code, error_body)
                            retry_after = response.headers.get("Retry-After", "5")
                            try:
                                wait = int(retry_after)
                            except ValueError:
                                wait = 5
                            await asyncio.sleep(wait)
                            continue

                        if response.status_code >= 500:
                            if attempt >= max_retries_server - 1:
                                raise LLMAPIError(response.status_code, error_body)
                            await asyncio.sleep(2 ** attempt)
                            continue

                        raise LLMAPIError(response.status_code, error_body)

                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                    if attempt >= max_retries_network - 1:
                        error_detail = (
                            f"{type(e).__name__}: "
                            f"{str(e) or 'Connection dropped or timed out'}"
                        )
                        raise LLMAPIError(0, error_detail) from e
                    await asyncio.sleep(2 ** attempt)

        raise LLMAPIError(0, "LLM request exhausted retry loop without a response")

    async def _parse_stream(
        self,
        response: httpx.Response,
        on_token: Callable[[str], None] | None,
    ) -> dict:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_call_buf: dict[int, dict] = {}
        provider_usage: dict[str, Any] | None = None

        reasoning_started = False
        content_started = False

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
                    logging.getLogger(__name__).debug(
                        "Failed to parse SSE data line: %s",
                        data[:200],
                    )
                    continue

                raw_usage = chunk.get("usage")
                if isinstance(raw_usage, dict):
                    provider_usage = raw_usage

                choices = chunk.get("choices")
                if not choices:
                    continue

                choice = choices[0] if isinstance(choices, list) else choices
                delta = choice.get("delta", {})

                if "reasoning_content" in delta and delta["reasoning_content"]:
                    token = delta["reasoning_content"]
                    reasoning_parts.append(token)
                    if on_token:
                        if not reasoning_started:
                            on_token("> Thinking...\n> ")
                            reasoning_started = True
                        on_token(token.replace("\n", "\n> "))

                if "content" in delta and delta["content"]:
                    token = delta["content"]
                    content_parts.append(token)
                    if on_token:
                        if reasoning_started and not content_started:
                            on_token("\n\n")
                        content_started = True
                        on_token(token)

                if "tool_calls" in delta:
                    for tool_call in delta["tool_calls"]:
                        idx = tool_call.get("index", 0)
                        if idx not in tool_call_buf:
                            tool_call_buf[idx] = {
                                "id": "",
                                "function": {"name": "", "arguments": ""},
                            }
                        if "id" in tool_call and tool_call["id"]:
                            tool_call_buf[idx]["id"] = tool_call["id"]
                        if "function" in tool_call:
                            function = tool_call["function"]
                            if function.get("name"):
                                tool_call_buf[idx]["function"]["name"] = function["name"]
                            if "arguments" in function:
                                tool_call_buf[idx]["function"]["arguments"] += function["arguments"]
        except httpx.HTTPError as e:
            if not content_parts and not tool_call_buf:
                raise LLMAPIError(0, f"Stream connection dropped or timed out: {e}") from e

        for idx in sorted(tool_call_buf.keys()):
            tool_call = tool_call_buf[idx]
            tool_call["type"] = "function"
            tool_calls.append(tool_call)

        result: dict = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if reasoning_parts:
            result["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            result["tool_calls"] = tool_calls
        if provider_usage is not None:
            result["_provider_usage"] = provider_usage

        return result
