from __future__ import annotations

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

    async def execute(self, action: str, **kwargs) -> ToolResult:
        state = GlobalState.get()

        if action == "add_task":
            description = kwargs.get("description", "")
            if not description:
                return ToolResult.fail("'description' is required for add_task action")
            dependencies = kwargs.get("dependencies", [])
            task_id = state.add_task(description, dependencies)
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
            state.update_task(task_id, status=new_status)
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
            state.add_summary(task_id, summary)
            state.update_task(task_id, status="done")
            return ToolResult.ok(f"Summary recorded for {task_id}")

        else:
            return ToolResult.fail(f"Unknown action: {action}")
