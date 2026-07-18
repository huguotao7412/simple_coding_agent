from __future__ import annotations

from typing import Any

from .base import BaseTool, ToolResult
from ..runs.task_state import GlobalState


class UpdateStateTool(BaseTool):
    name = "update_state"
    description = (
        "Update the global engineering ledger. Use this to add tasks, update "
        "Planner-owned task status, or record Planner summaries. Actor-owned "
        "terminal results are recorded by delegate and cannot be overwritten here."
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["add_task", "update_task", "add_summary"],
            "description": "What kind of state update to perform.",
        },
        "task_id": {
            "type": "string",
            "description": "Target task ID. Required for update_task and add_summary.",
        },
        "description": {
            "type": "string",
            "description": "Task description. Required for add_task.",
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of task_ids this task depends on.",
        },
        "status": {
            "type": "string",
            "enum": ["pending", "running", "done", "failed", "blocked"],
            "description": "New status. Required for update_task.",
        },
        "summary": {
            "type": "string",
            "description": "Planner summary text. Required for add_summary.",
        },
    }
    required_params = ["action"]

    def __init__(self, state: GlobalState | None = None):
        super().__init__()
        self._state = state

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.pop("action", ""))
        state = self._state or GlobalState.get()

        if action == "add_task":
            description = kwargs.get("description", "")
            if not description:
                return ToolResult.fail("'description' is required for add_task action")
            dependencies = kwargs.get("dependencies", [])
            task_id = await state.add_task(description, dependencies)
            return ToolResult.ok(f"Task registered: {task_id}")

        if action == "update_task":
            task_id = kwargs.get("task_id", "")
            if not task_id:
                return ToolResult.fail("'task_id' is required for update_task action")
            if task_id not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {task_id}")
            new_status = kwargs.get("status", "")
            if new_status not in ("pending", "running", "done", "failed", "blocked"):
                return ToolResult.fail(f"Invalid status: {new_status}")
            node = state.task_tree[task_id]
            if node.assigned_actor and new_status in {"done", "failed", "blocked"}:
                return ToolResult.fail(
                    "Actor-owned task terminal status can only be set by the "
                    "Actor runtime, not Planner update_state"
                )
            try:
                await state.update_task(task_id, status=new_status)
            except ValueError as error:
                return ToolResult.fail(str(error))
            return ToolResult.ok(f"Task {task_id} -> {new_status}")

        if action == "add_summary":
            task_id = kwargs.get("task_id", "")
            if not task_id:
                return ToolResult.fail("'task_id' is required for add_summary action")
            if task_id not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {task_id}")
            node = state.task_tree[task_id]
            if node.assigned_actor or node.actor_role:
                return ToolResult.fail(
                    "Actor-owned task summaries are recorded by delegate; Planner "
                    "cannot convert an Actor result to done with add_summary"
                )
            summary = kwargs.get("summary", "")
            if not summary:
                return ToolResult.fail("'summary' is required for add_summary action")
            await state.add_summary(task_id, summary)
            try:
                await state.update_task(task_id, status="done")
            except ValueError as error:
                return ToolResult.fail(str(error))
            return ToolResult.ok(f"Summary recorded for {task_id}")

        return ToolResult.fail(f"Unknown action: {action}")
