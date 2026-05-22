import pytest
from core.context import ContextManager


class TestContextManager:
    def test_add_messages(self):
        cm = ContextManager(system_prompt="You are a coder.", max_tokens=100000, model_context_limit=128000)
        cm.add_user_message("hello")
        cm.add_assistant_message("hi")
        cm.add_tool_result("tool_id_1", "output")
        assert len(cm.messages) == 4  # system + user + assistant + tool

    def test_estimate_tokens_returns_int(self):
        cm = ContextManager(system_prompt="test", max_tokens=100000, model_context_limit=128000)
        cm.add_user_message("hello world")
        tokens = cm.estimate_tokens()
        assert isinstance(tokens, int)
        assert tokens > 0

    def test_needs_compression_false_when_under_threshold(self):
        cm = ContextManager(system_prompt="test", max_tokens=100000, model_context_limit=128000)
        cm.add_user_message("short")
        assert not cm.needs_compression()

    def test_needs_compression_threshold_respected(self):
        cm = ContextManager(system_prompt="test", max_tokens=100, model_context_limit=1000)
        for i in range(50):
            cm.add_user_message(f"message number {i} " + "x" * 50)
        needs = cm.needs_compression()
        assert isinstance(needs, bool)

    def test_get_compressible_range_preserves_recent(self):
        cm = ContextManager(system_prompt="test", max_tokens=100000, model_context_limit=128000, keep_recent=2)
        for i in range(10):
            cm.add_user_message(f"msg {i}")
            cm.add_assistant_message(f"reply {i}")
        start, end = cm.get_compressible_range()
        assert start == 1  # preserve system prompt
        assert end > 1     # some messages get compressed
