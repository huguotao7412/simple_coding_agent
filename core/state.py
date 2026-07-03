from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, ClassVar


@dataclass
class TaskNode:
    task_id: str
    description: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    assigned_actor: str | None = None
    dependencies: list[str] = field(default_factory=list)
    result_summary: str | None = None
    diff: str | None = None  # unified diff from Actor's worktree changes


@dataclass
class ChangeRecord:
    type: str  # "task_added" | "task_updated" | "summary_added"
    task_id: str
    timestamp: float
    payload: dict


class GlobalState:
    _instance: ClassVar[GlobalState | None] = None

    def __init__(self):
        self.task_tree: dict[str, TaskNode] = {}
        self.change_log: list[ChangeRecord] = []
        self._last_consumed: int = 0

    @classmethod
    def get(cls) -> GlobalState:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def add_task(self, description: str, dependencies: list[str] | None = None) -> str:
        import uuid
        import time
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        self.task_tree[task_id] = TaskNode(
            task_id=task_id,
            description=description,
            dependencies=dependencies or [],
        )
        self.change_log.append(ChangeRecord(
            type="task_added", task_id=task_id,
            timestamp=time.time(), payload={"description": description},
        ))
        return task_id

    def update_task(self, task_id: str, **kwargs) -> None:
        import time
        node = self.task_tree[task_id]
        for key, value in kwargs.items():
            if hasattr(node, key):
                setattr(node, key, value)
        self.change_log.append(ChangeRecord(
            type="task_updated", task_id=task_id,
            timestamp=time.time(), payload=kwargs,
        ))

    def add_summary(self, task_id: str, summary: str, diff: str = "") -> None:
        import time
        self.task_tree[task_id].result_summary = summary
        self.task_tree[task_id].diff = diff or None
        self.change_log.append(ChangeRecord(
            type="summary_added", task_id=task_id,
            timestamp=time.time(), payload={"summary": summary, "diff": diff},
        ))

    def consume_changes(self) -> list[ChangeRecord]:
        new_changes = self.change_log[self._last_consumed:]
        self._last_consumed = len(self.change_log)
        return new_changes

    def snapshot(self) -> dict:
        return {
            "task_tree": {
                tid: {
                    "task_id": t.task_id,
                    "description": t.description,
                    "status": t.status,
                    "assigned_actor": t.assigned_actor,
                    "dependencies": t.dependencies,
                    "result_summary": t.result_summary,
                    "diff": (t.diff or "")[:500],  # truncate for context window
                }
                for tid, t in self.task_tree.items()
            },
            "change_count": len(self.change_log),
        }
