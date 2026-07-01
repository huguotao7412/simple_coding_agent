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
        """Rough token count estimation using UTF-8 byte length heuristic."""
        total = 0
        for msg in self.messages:
            for key, value in msg.items():
                if isinstance(value, str):
                    total += len(value.encode('utf-8', errors='ignore')) // 3
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            total += len(str(item).encode('utf-8', errors='ignore')) // 3
        return max(1, total)

    def needs_compression(self) -> bool:
        return self.estimate_tokens() > int(self.model_context_limit * self.compression_threshold)

    def get_compressible_range(self) -> tuple[int, int]:
        if len(self.messages) <= 1:
            return (1, 1)

            # 1. 常规策略：保留最近的几次 user 交互
        user_indices = [i for i, m in enumerate(self.messages) if m["role"] == "user"]
        if len(user_indices) > self.keep_recent:
            return (1, user_indices[-self.keep_recent])

        # 2. 兜底防爆策略：寻找不破坏 Tool 闭环的安全截断点
        safe_end = 1
        if len(self.messages) > 15:
            # 从后往前找，优先留足尾部上下文
            for i in range(len(self.messages) - 10, 1, -1):
                msg = self.messages[i]
                if msg["role"] == "assistant" and not msg.get("tool_calls"):
                    safe_end = i
                    break

            # 【新增修复】如果在长工具循环中找不到纯文本 assistant，强制选择一个最近的 assistant 切断
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

        # Build slim summary entries
        slim_entries: list[str] = []
        for m in messages_to_drop:
            role = m["role"]
            raw = (m.get("content") or "")
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

    _FENCE_RE = re.compile(r"```")
    _XML_TAG_RE = re.compile(r"<(scratchpad|completed_tasks|current_bugs|key_files_in_focus)\b")

    def _truncate_large_messages(self, msgs: list[dict], max_chars: int = 12000) -> None:
        """Smart truncation that preserves code fence integrity.

        - tool/assistant/user messages exceeding max_chars are head-tail truncated.
        - For assistant messages: avoids breaking inside ``` code fences.
        - For tool messages: finds natural break points to avoid corrupting output.
        """
        for msg in msgs:
            content = msg.get("content")
            if not isinstance(content, str) or len(content) <= max_chars:
                continue

            role = msg.get("role", "")
            keep_head = int(max_chars * 0.35)
            keep_tail = int(max_chars * 0.35)
            omitted = len(content) - keep_head - keep_tail

            trunc_marker = (
                f"\n\n... [System: {omitted} chars omitted to prevent context overflow] ...\n\n"
            )

            # --- Assistant messages: ensure code fence safety ---
            if role == "assistant":
                head = content[:keep_head]
                tail = content[-keep_tail:]
                head = self._close_open_fences(head)
                tail = self._reopen_closed_fences(tail)
                msg["content"] = head + trunc_marker + tail

            # --- Tool messages: truncate at natural boundaries ---
            elif role == "tool":
                head = content[:keep_head]
                tail = content[-keep_tail:]
                # Try to break at last newline in head to avoid mid-line cuts
                last_nl = head.rfind("\n")
                if last_nl > keep_head * 0.7:
                    head = head[:last_nl]
                first_nl = tail.find("\n")
                if first_nl != -1 and first_nl < keep_tail * 0.3:
                    tail = tail[first_nl:]
                msg["content"] = head + trunc_marker + tail

            # --- User messages: simple safe truncation ---
            else:
                msg["content"] = content[:keep_head] + trunc_marker + content[-keep_tail:]

    @staticmethod
    def _close_open_fences(text: str) -> str:
        """If there's an odd number of ``` in text, close the last open fence."""
        count = text.count("```")
        if count % 2 == 1:
            text += "\n```"
        return text

    @staticmethod
    def _reopen_closed_fences(text: str) -> str:
        """If there's an odd number of ``` in text, prepend an opening fence."""
        count = text.count("```")
        if count % 2 == 1:
            text = "```\n" + text
        return text
