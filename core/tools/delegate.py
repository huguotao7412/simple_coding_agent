from __future__ import annotations

import asyncio
import os

from .base import BaseTool, ToolResult
from ..state import GlobalState

MAX_CONCURRENT_ACTORS = 4

# TODO (Task 16): Import ACTOR_TOOLS from ..tools instead of defining locally.
# For now, define the Actor-accessible tool set here.
# Actors get read/write/edit/bash/search/list_dir/read_outline but NOT
# delegate (to prevent recursion) and NOT update_state (Planner-only).
from .read import ReadTool
from .write import WriteTool
from .edit import EditTool
from .bash import BashTool
from .search import SearchCodebaseTool
from .list_dir import ListDirTool
from .read_outline import ReadOutlineTool

ACTOR_TOOLS: list[type[BaseTool]] = [
    ReadTool,
    WriteTool,
    EditTool,
    BashTool,
    SearchCodebaseTool,
    ListDirTool,
    ReadOutlineTool,
]


class DelegateTool(BaseTool):
    name = "delegate"
    description = (
        "Dispatch multiple subtasks to independent Actor agents for concurrent execution. "
        "Each Actor runs with full tool access. "
        "Use this after you have decomposed a complex task into independent subtasks "
        "via update_state. Returns structured summaries from each Actor."
    )
    parameters = {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID from the global state (register via update_state first).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Specific, actionable task description for the Actor.",
                    },
                    "context_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to pre-read and inject into the Actor's context.",
                    },
                    "context_summaries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant summary snippets from previous Actors to inject as context.",
                    },
                },
                "required": ["task_id", "description"],
            },
        }
    }
    required_params = ["subtasks"]

    def __init__(self, llm_client, workspace_dir: str):
        super().__init__()
        self._llm = llm_client
        self._workspace_dir = workspace_dir

    async def execute(self, subtasks: list[dict], **kwargs) -> ToolResult:
        """Dispatch subtasks to Actors concurrently with asyncio gate."""
        from ..agent import ActorAgent
        from ..context import ContextManager

        # TODO (Task 15): Import ACTOR_SYSTEM_PROMPT from ..system_prompt.
        # For now fall back to SYSTEM_PROMPT (the Planner prompt).
        try:
            from ..system_prompt import ACTOR_SYSTEM_PROMPT
        except ImportError:
            from ..system_prompt import SYSTEM_PROMPT as ACTOR_SYSTEM_PROMPT

        state = GlobalState.get()

        # Validate all task_ids
        for st in subtasks:
            tid = st.get("task_id", "")
            if tid not in state.task_tree:
                return ToolResult.fail(
                    f"Unknown task_id: {tid}. Register via update_state first."
                )

        # Mark all as running
        for st in subtasks:
            state.update_task(st["task_id"], status="running")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)

        async def run_one(subtask: dict) -> dict:
            tid = subtask["task_id"]
            description = subtask["description"]
            context_files = subtask.get("context_files", [])
            context_summaries = subtask.get("context_summaries", [])

            async with semaphore:
                # Build injected context message
                context_parts = [f"## Task\n{description}"]
                if context_files:
                    context_parts.append("\n## Relevant Files")
                    for fp in context_files:
                        try:
                            abs_path = os.path.join(self._workspace_dir, fp)
                            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read()[:4000]
                            context_parts.append(f"\n### {fp}\n```\n{content}\n```")
                        except Exception:
                            context_parts.append(f"\n### {fp}\n(unable to read)")
                if context_summaries:
                    context_parts.append("\n## Context from Previous Actors")
                    for s in context_summaries:
                        context_parts.append(f"- {s}")

                injected_context = "\n".join(context_parts)

                # Build ContextManager with actor prompt + injected context
                actor_ctx = ContextManager(
                    system_prompt=ACTOR_SYSTEM_PROMPT,
                    max_tokens=self._llm.max_tokens,
                )
                actor_ctx.add_user_message(injected_context)

                actor = ActorAgent(
                    llm_client=self._llm,
                    context_manager=actor_ctx,
                    tools=[t() for t in ACTOR_TOOLS],
                    workspace_dir=self._workspace_dir,
                    actor_id=tid,
                    task_context=description,
                )

                try:
                    summary = await actor.run(description)
                    state.add_summary(tid, summary.key_findings or "Task completed.")
                    state.update_task(tid, status=summary.status)
                    return {
                        "task_id": tid,
                        "status": summary.status,
                        "files_modified": summary.files_modified,
                        "bugs_found": summary.bugs_found,
                        "key_findings": (summary.key_findings or "")[:500],
                        "suggested_next_steps": summary.suggested_next_steps,
                    }
                except Exception as e:
                    state.update_task(tid, status="failed")
                    state.add_summary(tid, f"ERROR: {e}")
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": str(e),
                    }

        # Concurrent execution
        results = await asyncio.gather(*[run_one(st) for st in subtasks])

        # Build return message
        lines = [f"Delegate complete: {len(results)} subtask(s) executed.\n"]
        for r in results:
            status_icon = "OK" if r["status"] == "done" else "FAIL"
            detail = r.get("key_findings", r.get("error", ""))[:200]
            lines.append(f"  [{status_icon}] {r['task_id']}: {detail}")
        return ToolResult.ok("\n".join(lines))
