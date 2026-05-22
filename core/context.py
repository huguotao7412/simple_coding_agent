from __future__ import annotations


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

    async def compress(self, llm_client, compression_model: str | None = None) -> None:
        """Summarize oldest messages using the LLM, replace them with a summary message."""
        start, end = self.get_compressible_range()
        if start >= end:
            return

        messages_to_summarize = self.messages[start:end]

        summary_prompt = (
            "Summarize the following conversation history concisely, "
            "preserving key decisions, file changes made, and unresolved tasks:\n\n"
        )
        summary_prompt += "\n".join(
            f"[{m['role']}]: {m.get('content', '')[:500]}"
            for m in messages_to_summarize
        )

        try:
            result = await llm_client.chat(
                messages=[{"role": "user", "content": summary_prompt}],
                tools=None,
                on_token=None,
            )
            summary = result.get("content", "Previous conversation summarized.")
        except Exception:
            summary = "(Conversation history compressed due to context limit.)"

        new_messages = self.messages[:start] + [
            {"role": "system", "content": f"[Conversation summary]: {summary}"}
        ] + self.messages[end:]
        self.messages = new_messages
