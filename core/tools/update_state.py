from __future__ import annotations

from typing import Any

from .base import BaseTool, ToolResult
from ..state import GlobalState


class UpdateStateTool(BaseTool):
    name = "update_state"
    description = (
        "Update the global engineering ledger. "
        "Use this to add tasks, update task status, or record Actor summaries. "
        "This is the Planner's working memory — keep it current."
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["add_task", "update_task", "add_summary"],
            "description": "What kind of state update to perform.",
        },
        "task_id": {
            "type": "string",
            "description": "Target task ID. Required for update_task and add_summary actions.",
        },
        "description": {
            "type": "string",
            "description": "Task description. Required for add_task action.",
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of task_id this task depends on. Optional for add_task.",
        },
        "status": {
            "type": "string",
            "enum": ["pending", "running", "done", "failed"],
            "description": "New status. Required for update_task action.",
        },
        "summary": {
            "type": "string",
            "description": "Actor summary text. Required for add_summary action.",
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

        elif action == "update_task":
            task_id = kwargs.get("task_id", "")
            if not task_id:
                return ToolResult.fail("'task_id' is required for update_task action")
            if task_id not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {task_id}")
            new_status = kwargs.get("status", "")
            if new_status not in ("pending", "running", "done", "failed"):
                return ToolResult.fail(f"Invalid status: {new_status}")
            await state.update_task(task_id, status=new_status)
            return ToolResult.ok(f"Task {task_id} -> {new_status}")

        elif action == "add_summary":
            task_id = kwargs.get("task_id", "")
            if not task_id:
                return ToolResult.fail("'task_id' is required for add_summary action")
            if task_id not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {task_id}")
            summary = kwargs.get("summary", "")
            if not summary:
                return ToolResult.fail("'summary' is required for add_summary action")
            await state.add_summary(task_id, summary)
            await state.update_task(task_id, status="done")
            return ToolResult.ok(f"Summary recorded for {task_id}")

        else:
            return ToolResult.fail(f"Unknown action: {action}")
