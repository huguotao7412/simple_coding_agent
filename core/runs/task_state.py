from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Literal, ClassVar, cast

from ..a2a_lite.models import AgentMessage


@dataclass
class TaskNode:
    task_id: str
    description: str
    status: Literal["pending", "running", "verifying", "done", "failed", "blocked"] = "pending"
    assigned_actor: str | None = None
    actor_role: str | None = None
    dependencies: list[str] = field(default_factory=list)
    result_summary: str | None = None
    diff: str | None = None  # unified diff from Actor's worktree changes
    files_modified: list[str] = field(default_factory=list)
    diff_artifact: str | None = None
    handoff_message: AgentMessage | None = None
    verification_passed: bool | None = None


@dataclass
class ChangeRecord:
    type: str  # "task_added" | "task_updated" | "summary_added"
    task_id: str
    timestamp: float
    payload: dict[str, Any]


class GlobalState:
    _instance: ClassVar[GlobalState | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self.task_tree: dict[str, TaskNode] = {}
        self.change_log: list[ChangeRecord] = []
        self._change_offset: int = 0
        self._last_consumed: int = 0
        self._lock = asyncio.Lock()

    @classmethod
    def get(cls) -> GlobalState:
        """Thread-safe singleton accessor.

        Uses double-checked locking to avoid contention after the first init.
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    async def add_task(self, description: str, dependencies: list[str] | None = None) -> str:
        import uuid
        import time
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        async with self._lock:
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

    async def update_task(self, task_id: str, **kwargs: Any) -> None:
        import time
        async with self._lock:
            node = self.task_tree[task_id]
            for key, value in kwargs.items():
                if hasattr(node, key):
                    setattr(node, key, value)
            self.change_log.append(ChangeRecord(
                type="task_updated", task_id=task_id,
                timestamp=time.time(), payload=kwargs,
            ))

    async def add_summary(
        self,
        task_id: str,
        summary: str,
        diff: str = "",
        files_modified: list[str] | None = None,
        diff_artifact: str | None = None,
        handoff_message: AgentMessage | None = None,
        verification_passed: bool | None = None,
    ) -> None:
        import time
        async with self._lock:
            self.task_tree[task_id].result_summary = summary
            self.task_tree[task_id].diff = diff or None
            if files_modified is not None:
                self.task_tree[task_id].files_modified = files_modified
            self.task_tree[task_id].diff_artifact = diff_artifact
            self.task_tree[task_id].handoff_message = handoff_message
            self.task_tree[task_id].verification_passed = verification_passed
            self.change_log.append(ChangeRecord(
                type="summary_added",
                task_id=task_id,
                timestamp=time.time(),
                payload={
                    "summary": summary,
                    "diff": diff,
                    "files_modified": files_modified or [],
                    "diff_artifact": diff_artifact,
                    "handoff_message": (
                        handoff_message.to_dict() if handoff_message else None
                    ),
                    "verification_passed": verification_passed,
                },
            ))

    async def consume_changes(self) -> list[ChangeRecord]:
        async with self._lock:
            new_changes = self.change_log[self._last_consumed:]
            self._last_consumed = len(self.change_log)
        return new_changes

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> GlobalState:
        """Reconstruct task state from a trusted durable snapshot payload."""
        state = cls()
        raw_tree = snapshot.get("task_tree", {})
        if not isinstance(raw_tree, dict):
            raise ValueError("task snapshot task_tree must be an object")
        valid_statuses = {
            "pending", "running", "verifying", "done", "failed", "blocked"
        }
        for task_id, raw_node in raw_tree.items():
            if not isinstance(raw_node, dict):
                raise ValueError(f"task snapshot entry {task_id} must be an object")
            status = str(raw_node.get("status", "pending"))
            if status not in valid_statuses:
                raise ValueError(f"invalid task status in checkpoint: {status}")
            dependencies = raw_node.get("dependencies", [])
            files_modified = raw_node.get("files_modified", [])
            if not isinstance(dependencies, list) or not isinstance(files_modified, list):
                raise ValueError("task dependencies and files_modified must be lists")
            verification_passed = raw_node.get("verification_passed")
            if verification_passed is not None and not isinstance(
                verification_passed, bool
            ):
                raise ValueError("task verification_passed must be a boolean or null")
            normalized_id = str(task_id)
            state.task_tree[normalized_id] = TaskNode(
                task_id=str(raw_node.get("task_id", normalized_id)),
                description=str(raw_node.get("description", "")),
                status=cast(Any, status),
                assigned_actor=(
                    str(raw_node["assigned_actor"])
                    if raw_node.get("assigned_actor") is not None
                    else None
                ),
                dependencies=[str(value) for value in dependencies],
                result_summary=(
                    str(raw_node["result_summary"])
                    if raw_node.get("result_summary") is not None
                    else None
                ),
                diff=(
                    str(raw_node["diff"])
                    if raw_node.get("diff") is not None
                    else None
                ),
                files_modified=[str(value) for value in files_modified],
                diff_artifact=(
                    str(raw_node["diff_artifact"])
                    if raw_node.get("diff_artifact") is not None
                    else None
                ),
                handoff_message=(
                    AgentMessage.from_dict(raw_node["handoff_message"])
                    if isinstance(raw_node.get("handoff_message"), dict)
                    else None
                ),
                actor_role=(
                    str(raw_node["actor_role"])
                    if raw_node.get("actor_role") is not None
                    else None
                ),
                verification_passed=(
                    verification_passed
                    if verification_passed is not None
                    else None
                ),
            )
        state._change_offset = int(snapshot.get("change_count", 0) or 0)
        return state

    async def snapshot(self, *, truncate_diffs: bool = True) -> dict[str, Any]:
        async with self._lock:
            return {
                "task_tree": {
                    tid: {
                        "task_id": t.task_id,
                        "description": t.description,
                        "status": t.status,
                        "assigned_actor": t.assigned_actor,
                        "actor_role": t.actor_role,
                        "dependencies": t.dependencies,
                        "result_summary": t.result_summary,
                        "diff": (
                            (t.diff or "")[:500]
                            if truncate_diffs
                            else t.diff
                        ),
                        "files_modified": t.files_modified,
                        "diff_artifact": t.diff_artifact,
                        "handoff_message": (
                            t.handoff_message.to_dict() if t.handoff_message else None
                        ),
                        "verification_passed": t.verification_passed,
                    }
                    for tid, t in self.task_tree.items()
                },
                "change_count": self._change_offset + len(self.change_log),
            }
