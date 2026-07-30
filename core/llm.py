from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from .model_names import DEFAULT_MODEL, normalize_model_name

try:
    from deepseek_tokenizer import ds_token
except ImportError:  # pragma: no cover
    ds_token = None

from .exceptions import LLMAPIError


logger = logging.getLogger(__name__)


class LLMClient:
    """Async OpenAI-compatible API client with streaming support."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = DEFAULT_MODEL,
        max_tokens: int = 128000,
        max_output_tokens: int = 8192,
        read_timeout_seconds: float = 120.0,
    ):
        if max_tokens <= 0 or max_output_tokens <= 0:
            raise ValueError("token limits must be positive")
        if read_timeout_seconds <= 0:
            raise ValueError("read timeout must be positive")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = normalize_model_name(model)
        # max_tokens is the context budget used by ContextManager. Keep the
        # per-response generation cap separate so a single tool turn cannot
        # consume the entire context window in hidden reasoning.
        self.max_tokens = max_tokens
        self.max_output_tokens = min(max_output_tokens, max_tokens)
        self.read_timeout_seconds = read_timeout_seconds
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
            "max_tokens": self.max_output_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        timeout_config = httpx.Timeout(
            connect=15.0,
            read=self.read_timeout_seconds,
            write=30.0,
            pool=15.0,
        )
        max_retries_network = 3
        max_retries_server = 4
        max_retries_rate = 5
        request_started = time.monotonic()
        logger.info(
            "LLM request started model=%s messages=%d tools=%d max_output_tokens=%d",
            self.model,
            len(messages),
            len(tools or []),
            self.max_output_tokens,
        )

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
                            logger.info(
                                "LLM request completed model=%s duration_ms=%d "
                                "tool_calls=%d",
                                self.model,
                                int((time.monotonic() - request_started) * 1000),
                                len(result.get("tool_calls") or []),
                            )
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
                            logger.warning(
                                "LLM rate limited; retrying attempt=%d wait_seconds=%d",
                                attempt + 1,
                                wait,
                            )
                            await asyncio.sleep(wait)
                            continue

                        if response.status_code >= 500:
                            if attempt >= max_retries_server - 1:
                                raise LLMAPIError(response.status_code, error_body)
                            wait = 2 ** attempt
                            logger.warning(
                                "LLM server error status=%d; retrying attempt=%d "
                                "wait_seconds=%d",
                                response.status_code,
                                attempt + 1,
                                wait,
                            )
                            await asyncio.sleep(wait)
                            continue

                        raise LLMAPIError(response.status_code, error_body)

                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                    if attempt >= max_retries_network - 1:
                        error_detail = (
                            f"{type(e).__name__}: "
                            f"{str(e) or 'Connection dropped or timed out'}"
                        )
                        raise LLMAPIError(0, error_detail) from e
                    wait = 2 ** attempt
                    logger.warning(
                        "LLM network error %s; retrying attempt=%d wait_seconds=%d",
                        type(e).__name__,
                        attempt + 1,
                        wait,
                    )
                    await asyncio.sleep(wait)

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
        stream_started = time.monotonic()
        last_activity_log = stream_started
        received_event = False
        stream_completed = False

        try:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    stream_completed = True
                    break

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logging.getLogger(__name__).debug(
                        "Failed to parse SSE data line: %s",
                        data[:200],
                    )
                    continue
                now = time.monotonic()
                if not received_event:
                    received_event = True
                    last_activity_log = now
                    logger.info(
                        "LLM stream connected model=%s first_event_ms=%d",
                        self.model,
                        int((now - stream_started) * 1000),
                    )
                elif now - last_activity_log >= 15:
                    logger.info(
                        "LLM stream active model=%s elapsed_seconds=%d "
                        "visible_chunks=%d reasoning_chunks=%d",
                        self.model,
                        int(now - stream_started),
                        len(content_parts),
                        len(reasoning_parts),
                    )
                    last_activity_log = now

                raw_usage = chunk.get("usage")
                if isinstance(raw_usage, dict):
                    provider_usage = raw_usage

                choices = chunk.get("choices")
                if not choices:
                    continue

                choice = choices[0] if isinstance(choices, list) else choices
                delta = choice.get("delta", {})
                if choice.get("finish_reason") is not None:
                    stream_completed = True

                if "reasoning_content" in delta and delta["reasoning_content"]:
                    token = delta["reasoning_content"]
                    reasoning_parts.append(token)

                if "content" in delta and delta["content"]:
                    token = delta["content"]
                    content_parts.append(token)
                    if on_token:
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
            raise LLMAPIError(
                0,
                f"Stream connection dropped or timed out: {type(e).__name__}: {e}",
            ) from e

        if not stream_completed:
            raise LLMAPIError(
                0,
                "Stream ended before a finish signal; partial output was discarded",
            )

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
