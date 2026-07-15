from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast


A2A_LITE_SCHEMA_VERSION = "a2a-lite/1.0"
ArtifactKind = Literal["patch", "verification", "report", "file"]
MessageKind = Literal["task.completed", "task.failed"]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class ArtifactRef:
    """Typed artifact reference with an optional content-integrity digest."""

    artifact_id: str
    kind: ArtifactKind
    uri: str
    media_type: str
    producer_task_id: str
    sha256: str = ""
    description: str = ""

    @classmethod
    def create(
        cls,
        *,
        kind: ArtifactKind,
        uri: str,
        media_type: str,
        producer_task_id: str,
        content: str | bytes = b"",
        description: str = "",
    ) -> ArtifactRef:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(raw).hexdigest() if raw else ""
        identity = f"{producer_task_id}\0{kind}\0{uri}\0{digest}"
        artifact_id = "artifact_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        return cls(
            artifact_id=artifact_id,
            kind=kind,
            uri=uri,
            media_type=media_type,
            producer_task_id=producer_task_id,
            sha256=digest,
            description=description,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "uri": self.uri,
            "media_type": self.media_type,
            "producer_task_id": self.producer_task_id,
            "sha256": self.sha256,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ArtifactRef:
        kind = str(value.get("kind", "file"))
        if kind not in {"patch", "verification", "report", "file"}:
            raise ValueError(f"unsupported A2A_lite artifact kind: {kind}")
        return cls(
            artifact_id=str(value.get("artifact_id", "")),
            kind=cast(ArtifactKind, kind),
            uri=str(value.get("uri", "")),
            media_type=str(value.get("media_type", "application/octet-stream")),
            producer_task_id=str(value.get("producer_task_id", "")),
            sha256=str(value.get("sha256", "")),
            description=str(value.get("description", "")),
        )


@dataclass(frozen=True)
class AgentHandoff:
    """Structured semantic context passed from an Actor to its consumers."""

    findings: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": list(self.findings),
            "decisions": list(self.decisions),
            "constraints": list(self.constraints),
            "unresolved_questions": list(self.unresolved_questions),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentHandoff:
        raw_artifacts = value.get("artifacts", [])
        artifacts = (
            tuple(
                ArtifactRef.from_dict(item)
                for item in raw_artifacts
                if isinstance(item, dict)
            )
            if isinstance(raw_artifacts, list)
            else ()
        )
        return cls(
            findings=_string_tuple(value.get("findings")),
            decisions=_string_tuple(value.get("decisions")),
            constraints=_string_tuple(value.get("constraints")),
            unresolved_questions=_string_tuple(value.get("unresolved_questions")),
            artifacts=artifacts,
        )


@dataclass(frozen=True)
class AgentMessage:
    """Versioned A2A_lite envelope persisted with the run task ledger."""

    message_id: str
    schema_version: str
    kind: MessageKind
    run_id: str
    task_id: str
    sender_id: str
    recipient_id: str
    correlation_id: str
    created_at: float
    handoff: AgentHandoff

    @classmethod
    def handoff_message(
        cls,
        *,
        run_id: str,
        task_id: str,
        sender_id: str,
        recipient_id: str,
        handoff: AgentHandoff,
        failed: bool = False,
    ) -> AgentMessage:
        return cls(
            message_id=f"msg_{uuid.uuid4().hex}",
            schema_version=A2A_LITE_SCHEMA_VERSION,
            kind="task.failed" if failed else "task.completed",
            run_id=run_id,
            task_id=task_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            correlation_id=task_id,
            created_at=time.time(),
            handoff=handoff,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "schema_version": self.schema_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "handoff": self.handoff.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def to_prompt_json(
        self,
        *,
        max_items: int = 20,
        max_text_chars: int = 2000,
    ) -> str:
        """Serialize a bounded envelope for an LLM observation or prompt."""
        if max_items <= 0 or max_text_chars <= 0:
            raise ValueError("A2A_lite prompt limits must be positive")

        def bounded(values: tuple[str, ...]) -> list[str]:
            return [value[:max_text_chars] for value in values[:max_items]]

        payload = self.to_dict()
        payload["handoff"] = {
            "findings": bounded(self.handoff.findings),
            "decisions": bounded(self.handoff.decisions),
            "constraints": bounded(self.handoff.constraints),
            "unresolved_questions": bounded(self.handoff.unresolved_questions),
            "artifacts": [
                {
                    **artifact.to_dict(),
                    "uri": artifact.uri[:max_text_chars],
                    "description": artifact.description[:max_text_chars],
                }
                for artifact in self.handoff.artifacts[:max_items]
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentMessage:
        version = str(value.get("schema_version", ""))
        if version != A2A_LITE_SCHEMA_VERSION:
            raise ValueError(f"unsupported A2A_lite schema version: {version}")
        kind = str(value.get("kind", ""))
        if kind not in {"task.completed", "task.failed"}:
            raise ValueError(f"unsupported A2A_lite message kind: {kind}")
        raw_handoff = value.get("handoff", {})
        if not isinstance(raw_handoff, dict):
            raise ValueError("A2A_lite handoff must be an object")
        return cls(
            message_id=str(value.get("message_id", "")),
            schema_version=version,
            kind=cast(MessageKind, kind),
            run_id=str(value.get("run_id", "")),
            task_id=str(value.get("task_id", "")),
            sender_id=str(value.get("sender_id", "")),
            recipient_id=str(value.get("recipient_id", "")),
            correlation_id=str(value.get("correlation_id", "")),
            created_at=float(value.get("created_at", 0.0)),
            handoff=AgentHandoff.from_dict(raw_handoff),
        )


__all__ = [
    "A2A_LITE_SCHEMA_VERSION",
    "AgentHandoff",
    "AgentMessage",
    "ArtifactKind",
    "ArtifactRef",
    "MessageKind",
]
