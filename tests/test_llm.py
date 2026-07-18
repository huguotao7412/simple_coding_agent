from __future__ import annotations

import pytest

from core.llm import LLMClient


def test_llm_client_upgrades_deepseek_legacy_aliases_to_pro():
    assert LLMClient(api_key="test", model="deepseek-chat").model == "deepseek-v4-pro"
    assert LLMClient(api_key="test", model="deepseek/deepseek-reasoner").model == "deepseek-v4-pro"
    assert LLMClient(api_key="test", model="deepseek-v4-flash").model == "deepseek-v4-flash"


class FakeStreamingResponse:
    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}'
        yield "data: [DONE]"


@pytest.mark.asyncio
async def test_parse_stream_preserves_provider_usage_chunk_without_choices():
    client = LLMClient(api_key="test")

    result = await client._parse_stream(FakeStreamingResponse(), on_token=None)

    assert result["content"] == "ok"
    assert result["_provider_usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 2,
        "total_tokens": 13,
    }


class FakeReasoningStreamingResponse:
    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"reasoning_content":"internal"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"visible"}}]}'
        yield "data: [DONE]"


@pytest.mark.asyncio
async def test_parse_stream_does_not_emit_reasoning_tokens_to_callback():
    client = LLMClient(api_key="test")
    tokens: list[str] = []

    result = await client._parse_stream(FakeReasoningStreamingResponse(), tokens.append)

    assert result["reasoning_content"] == "internal"
    assert result["content"] == "visible"
    assert tokens == ["visible"]
