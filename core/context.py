from __future__ import annotations

import re


class ContextManager:
    """Manages the conversation message list, token estimation, and compression."""

    def __init__(
        self,
        system_prompt: str,
        max_tokens: int = 128000,
        model_context_limit: int = 128000,
        compression_threshold: float = 0.8,
        keep_recent: int = 5,
    ):
        self.max_tokens = max_tokens
        self.model_context_limit = model_context_limit
        self.compression_threshold = compression_threshold
        self.keep_recent = keep_recent
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt}
        ]

    _SCRATCHPAD_RE = re.compile(r"<scratchpad>.*?</scratchpad>", re.DOTALL)

    @classmethod
    def _extract_last_scratchpad(cls, messages: list[dict]) -> str | None:
        """Extract the last scratchpad block from a list of messages.

        Scans in reverse order to find the most recent scratchpad.
        Returns the full XML block string, or None if not found.
        """
        for msg in reversed(messages):
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            matches = list(cls._SCRATCHPAD_RE.finditer(content))
            if matches:
                return matches[-1].group(0)
        return None

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(
        self,
        content: str | None,
        tool_calls: list[dict] | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        msg: dict = {"role": "assistant", "content": content}
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def estimate_tokens(self) -> int:
        """Rough token count estimation: ~4 chars per token."""
        total = 0
        for msg in self.messages:
            for key, value in msg.items():
                if isinstance(value, str):
                    total += len(value) // 4
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            total += len(str(item)) // 4
        return max(1, total)

    def needs_compression(self) -> bool:
        return self.estimate_tokens() > int(self.model_context_limit * self.compression_threshold)

    def get_compressible_range(self) -> tuple[int, int]:
        """Return (start, end) indices of messages to compress.

        Preserves system prompt (index 0) and last `keep_recent` turns.
        """
        if len(self.messages) <= 1 + self.keep_recent * 2:
            return (1, 1)

        user_indices = [
            i for i, m in enumerate(self.messages)
            if m["role"] == "user"
        ]
        if len(user_indices) <= self.keep_recent:
            return (1, 1)

        end = user_indices[-self.keep_recent]
        return (1, end)

    def compress(self) -> None:
        """Drop oldest messages via sliding window, preserving system prompt and scratchpad.

        No LLM summarization — pure truncation keeps the prefix static so KV caches
        stay warm, and exact code references (line numbers, stack traces) are never
        degraded by a lossy summary.
        """
        start, end = self.get_compressible_range()
        if start >= end:
            return

        # --- Extract latest scratchpad from the window being dropped ---
        messages_to_drop = self.messages[start:end]
        saved_scratchpad = self._extract_last_scratchpad(messages_to_drop)

        # --- Reassemble: system prompt -> scratchpad (if found) -> recent tail ---
        tail = self.messages[end:]
        new_messages = self.messages[:start]  # Keep system prompt (index 0)

        if saved_scratchpad:
            new_messages.append({
                "role": "system",
                "content": f"[Engineering Scratchpad]:\n{saved_scratchpad}",
            })

        new_messages.extend(tail)
        self.messages = new_messages
