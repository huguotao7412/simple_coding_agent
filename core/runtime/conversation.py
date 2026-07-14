from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any

from ..tools.base import DEFAULT_TOKEN_BUDGET, semantic_truncate


class ContextManager:
    """Manages the conversation message list, token estimation, and compression."""

    def __init__(
        self,
        system_prompt: str,
        max_tokens: int = 128000,
        model_context_limit: int = 128000,
        compression_threshold: float = 0.8,
        warning_threshold: float = 0.7,
        keep_recent: int = 5,
    ):
        self.max_tokens = max_tokens
        self.model_context_limit = model_context_limit
        self.compression_threshold = compression_threshold
        self.warning_threshold = warning_threshold
        self.keep_recent = keep_recent
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self._result_hashes: set[str] = set()
        self._max_hash_cache = 50

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_system_message(self, content: str) -> None:
        """Append durable per-turn system context such as a task assessment."""
        self.messages.append({"role": "system", "content": content})

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
        if len(content) > DEFAULT_TOKEN_BUDGET * 3:
            content, _ = semantic_truncate(content, token_budget=DEFAULT_TOKEN_BUDGET)

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if content_hash in self._result_hashes:
            self.messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": "[Same result as previous call, omitted]",
            })
            return

        self._result_hashes.add(content_hash)
        if len(self._result_hashes) > self._max_hash_cache:
            self._result_hashes.clear()

        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def restore_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace conversation state from a complete durable checkpoint."""
        if messages and messages[0].get("role") != "system":
            raise ValueError("restored messages must begin with a system message")
        self.messages = deepcopy(messages)
        self._result_hashes.clear()
        tool_results: list[str] = []
        for message in self.messages:
            content = message.get("content")
            if message.get("role") == "tool" and isinstance(content, str):
                tool_results.append(content)
        for content in tool_results[-self._max_hash_cache:]:
            self._result_hashes.add(hashlib.sha256(content.encode()).hexdigest())

    def estimate_tokens(self, llm_client) -> int:
        """Precise token count using LLM client's tokenizer."""
        return llm_client.count_messages_tokens(self.messages)

    def needs_compression(self, llm_client) -> bool:
        return self.estimate_tokens(llm_client) > int(
            self.model_context_limit * self.compression_threshold
        )

    def needs_proactive_compression(self, llm_client) -> bool:
        """Return true when large messages should be trimmed before summarizing."""
        return self.estimate_tokens(llm_client) > int(
            self.model_context_limit * self.warning_threshold
        )

    def _lightweight_compress(self) -> None:
        """Truncate large messages without calling the LLM for summarization."""
        self._truncate_large_messages(self.messages, max_chars=8000)

    def get_compressible_range(self) -> tuple[int, int]:
        if len(self.messages) <= 1:
            return (1, 1)

        user_indices = [i for i, m in enumerate(self.messages) if m["role"] == "user"]
        if len(user_indices) > self.keep_recent:
            return (1, user_indices[-self.keep_recent])

        safe_end = 1
        if len(self.messages) > 15:
            for i in range(len(self.messages) - 10, 1, -1):
                msg = self.messages[i]
                if msg["role"] == "assistant" and not msg.get("tool_calls"):
                    safe_end = i
                    break

            if safe_end == 1:
                for i in range(len(self.messages) - 10, 1, -1):
                    if self.messages[i]["role"] == "assistant":
                        safe_end = i
                        break

        return (1, safe_end)

    async def compress(self, llm_client) -> None:
        start, end = self.get_compressible_range()
        if start >= end:
            self._truncate_large_messages(self.messages)
            return

        messages_to_drop = self.messages[start:end]
        slim_entries: list[str] = []
        for message in messages_to_drop:
            role = message["role"]
            raw = message.get("content") or ""
            stripped = re.sub(r"```[^`]*```", "[code block omitted]", raw, flags=re.DOTALL)
            if role == "tool":
                snippet = stripped[:80].replace("\n", " ")
                slim_entries.append(f"[{role}]: {snippet}...")
            else:
                snippet = stripped[:150].replace("\n", " ")
                slim_entries.append(f"[{role}]: {snippet}")

        summary_prompt = (
            "Summarize the following conversation history concisely, "
            "preserving key file paths, decisions, and bugs:\n\n"
            + "\n".join(slim_entries)
        )

        try:
            result = await llm_client.chat([{"role": "user", "content": summary_prompt[:8000]}])
            summary = result.get("content", "Previous conversation summarized.")
        except Exception:
            summary = "(Conversation compressed but summary failed due to error.)"

        tail = self.messages[end:]
        self._truncate_large_messages(tail)

        new_messages = self.messages[:start]
        new_messages.append({"role": "system", "content": f"[Conversation summary]: {summary}"})
        new_messages.extend(tail)
        self.messages = new_messages
        self._result_hashes.clear()

    def _truncate_large_messages(self, msgs: list[dict], max_chars: int = 12000) -> None:
        """Head-tail truncate large messages while preserving code fence integrity."""
        for msg in msgs:
            content = msg.get("content")
            if not isinstance(content, str) or len(content) <= max_chars:
                continue

            role = msg.get("role", "")
            keep_head = int(max_chars * 0.35)
            keep_tail = int(max_chars * 0.35)
            omitted = len(content) - keep_head - keep_tail
            trunc_marker = (
                f"\n\n... [System: omitted {omitted} characters to prevent context overflow] ...\n\n"
            )

            if role == "assistant":
                head = self._close_open_fences(content[:keep_head])
                tail = self._reopen_closed_fences(content[-keep_tail:])
                msg["content"] = head + trunc_marker + tail
            elif role == "tool":
                head = content[:keep_head]
                tail = content[-keep_tail:]
                last_nl = head.rfind("\n")
                if last_nl > keep_head * 0.7:
                    head = head[:last_nl]
                first_nl = tail.find("\n")
                if first_nl != -1 and first_nl < keep_tail * 0.3:
                    tail = tail[first_nl:]
                msg["content"] = head + trunc_marker + tail
            else:
                msg["content"] = content[:keep_head] + trunc_marker + content[-keep_tail:]

    @staticmethod
    def _close_open_fences(text: str) -> str:
        """If there is an odd number of ``` markers, close the last open fence."""
        if text.count("```") % 2 == 1:
            text += "\n```"
        return text

    @staticmethod
    def _reopen_closed_fences(text: str) -> str:
        """If there is an odd number of ``` markers, prepend an opening fence."""
        if text.count("```") % 2 == 1:
            text = "```\n" + text
        return text
