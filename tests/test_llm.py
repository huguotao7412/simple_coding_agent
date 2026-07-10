from __future__ import annotations

import pytest

from core.llm import LLMClient


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
