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
        saved_scratchpad = self._extract_last_scratchpad(messages_to_drop)

        # 构造摘要 Prompt 并调用 LLM
        summary_prompt = "Summarize the following conversation history concisely, preserving key file paths, decisions, and bugs:\n\n"
        summary_prompt += "\n".join(f"[{m['role']}]: {(m.get('content') or '')[:500]}" for m in messages_to_drop)

        try:
            # 利用传入的 llm_client 进行轻量级摘要
            result = await llm_client.chat([{"role": "user", "content": summary_prompt[:16000]}])
            summary = result.get("content", "Previous conversation summarized.")
        except Exception:
            summary = "(Conversation compressed but summary failed due to error.)"

        tail = self.messages[end:]
        self._truncate_large_messages(tail)

        new_messages = self.messages[:start]
        if saved_scratchpad:
            new_messages.append({"role": "system", "content": f"[Engineering Scratchpad]:\n{saved_scratchpad}"})

        new_messages.append({"role": "system", "content": f"[Conversation summary]: {summary}"})
        new_messages.extend(tail)
        self.messages = new_messages

    def _truncate_large_messages(self, msgs: list[dict], max_chars: int = 12000) -> None:
        """[NEW] 强制截断保留窗口内的过长文本，彻底根除 Context Overflow 死锁。"""
        for msg in msgs:
            if msg.get("role") in ("tool", "user", "assistant") and isinstance(msg.get("content"), str):
                if len(msg["content"]) > max_chars:
                    half = int(max_chars * 0.4)
                    omitted = len(msg["content"]) - (half * 2)
                    msg["content"] = (
                            msg["content"][:half] +
                            f"\n\n... [System: Message exceeded {max_chars} chars. {omitted} chars aggressively removed to prevent memory overflow] ...\n\n" +
                            msg["content"][-half:]
                    )
