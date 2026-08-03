from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.events import AgentEvent
from core.paths import safe_state_component, touch_workspace_state
from core.adapters.mcp import mcp_sdk_version
from core.mcp.client import BASH_MCP_VERSION, MCP_SERVER_FILESYSTEM_VERSION
from core.security.redaction import redact_text


FILE_ARG_KEYS = ("path", "file_path", "filepath", "source", "destination")


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    success: bool | None = None
    detail: str = ""


@dataclass
class RunReport:
    """Compact audit summary for one CLI agent run."""

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    files_referenced: set[str] = field(default_factory=set)
    actor_status_counts: dict[str, int] = field(default_factory=dict)
    actor_failure_categories: dict[str, int] = field(default_factory=dict)
    tool_provider_warnings: list[str] = field(default_factory=list)
    compactions: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_estimated: bool = False
    guardrail_prompt_tokens: int = 0
    guardrail_completion_tokens: int = 0
    guardrail_calls: int = 0
    guardrail_failures: int = 0
    guardrail_tripwires: int = 0
    guardrail_latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    final_output: str = ""
    task_assessment: dict[str, Any] | None = None
    execution_policy: dict[str, Any] | None = None
    sandbox_backends: set[str] = field(default_factory=set)
    isolated_execution_observed: bool = False
    first_edit_tool_call_index: int | None = None
    report_run_id: str = ""
    diagnostic_log_path: str = ""
    _has_token_stats: bool = field(default=False, init=False, repr=False)

    def observe(self, event: AgentEvent) -> None:
        if event.type == "task_assessment":
            self._record_task_assessment(event.content)

        elif event.type == "execution_policy":
            self._record_execution_policy(event.content)

        elif event.type == "sandbox_execution":
            self._record_sandbox_execution(event.content)

        elif event.type == "tool_call":
            args = dict(event.tool_args or {})
            self.tool_calls.append(ToolCallRecord(name=event.tool_name or "tool", args=args))
            if (
                self.first_edit_tool_call_index is None
                and event.tool_name in {"edit_file", "write_file", "apply_patch"}
            ):
                self.first_edit_tool_call_index = len(self.tool_calls)
            self._record_file_args(args)

        elif event.type == "tool_result":
            if not self.tool_calls:
                self.tool_calls.append(ToolCallRecord(name=event.tool_name or "tool"))
            record = self._matching_tool_call(event.tool_name or "")
            record.name = event.tool_name or record.name
            if event.tool_result is None:
                record.success = False
                return
            record.success = event.tool_result.success
            record.detail = event.tool_result.content if event.tool_result.success else (event.tool_result.error or "")

        elif event.type == "actor_update":
            self._record_actor_update(event.content)

        elif event.type == "compaction":
            self.compactions += 1

        elif event.type == "model_usage" and not self._has_token_stats:
            self._record_model_usage(event)

        elif event.type == "token_stats":
            self._record_token_stats(event)
            self._has_token_stats = True

        elif event.type in {"content_guard_result", "content_guard_error"}:
            self._record_guardrail_usage(event)

        elif event.type == "error":
            if event.content:
                self.errors.append(str(redact_text(event.content).value))

        elif event.type == "tool_provider_warning":
            if event.content:
                self.tool_provider_warnings.append(event.content)

        elif event.type == "done":
            self.final_output = event.content or ""

    @property
    def failed_tool_count(self) -> int:
        return sum(1 for call in self.tool_calls if call.success is False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def observed_test_commands(self) -> list[str]:
        commands: list[str] = []
        for call in self.tool_calls:
            command = call.args.get("command")
            if not isinstance(command, str):
                continue
            lowered = command.lower()
            if "pytest" in lowered or " test" in lowered or lowered.startswith("test"):
                commands.append(command)
        return commands

    @property
    def outcome(self) -> str:
        if self.cancelled:
            return "cancelled"
        return "failed" if self.errors or self.failed_tool_count else "completed"

    def mark_cancelled(self) -> None:
        self.cancelled = True

    def to_markdown(self) -> str:
        """Render a deterministic audit report suitable for eval checks."""
        lines = [
            "# Simple Coding Agent Final Report",
            "",
            "## Outcome",
            "",
            f"Status: {self.outcome}",
            "",
        ]

        if self.task_assessment:
            hints = self.task_assessment.get("execution_hints", {})
            reasons = self.task_assessment.get("reasons", [])
            lines.extend([
                "## Task Assessment",
                "",
                f"- Strategy: {self.task_assessment.get('strategy', 'unknown')}",
                f"- Intent: {self.task_assessment.get('intent', 'unknown')}",
                f"- Complexity: {self.task_assessment.get('complexity', 'unknown')}",
                f"- Risk: {self.task_assessment.get('risk', 'unknown')}",
                f"- Max actors: {hints.get('max_actors', 'unknown')}",
                f"- Human approval required: {bool(hints.get('requires_human_approval', False))}",
            ])
            if isinstance(reasons, list):
                lines.extend(f"- Reason: {reason}" for reason in reasons if isinstance(reason, str))
            lines.append("")

        if self.execution_policy:
            budget = self.execution_policy.get("budget", {})
            lines.extend([
                "## Enforced Execution Policy",
                "",
                f"- Strategy: {self.execution_policy.get('strategy', 'unknown')}",
                f"- Max actors: {self.execution_policy.get('max_actors', 'unknown')}",
                f"- Allowed roles: {', '.join(self.execution_policy.get('allowed_actor_roles', [])) or 'none'}",
                f"- Quality gates required: {bool(self.execution_policy.get('require_quality_gates', False))}",
                f"- Human approval satisfied: {bool(self.execution_policy.get('human_approved', False)) or not bool(self.execution_policy.get('requires_human_approval', False))}",
                f"- Model-call budget: {budget.get('max_model_calls', 'unknown')}",
                f"- Token budget: {budget.get('max_total_tokens', 'unknown')}",
                f"- Active wall-time budget: {budget.get('max_wall_time_seconds', 'unknown')}s",
                "",
            ])

        primary_failure = self._primary_failure()
        if primary_failure:
            primary = primary_failure
            phase = primary.split(":", 1)[0] if ":" in primary else "runtime"
            category = self._failure_category(primary)
            provider = self._provider_name(primary)
            exception_type = self._exception_type(primary)
            actor_started = bool(self.actor_status_counts)
            diff_produced = bool(self.files_referenced and self.first_edit_tool_call_index)
            verification_executed = bool(self.observed_test_commands)
            lines.extend([
                "## Primary Failure",
                "",
                f"- Primary failure: {primary}",
                f"- Failure phase: {phase}",
                f"- Failure category: {category}",
                f"- Exception type: {exception_type}",
                f"- Provider/server: {provider}",
                f"- MCP Python SDK version: {mcp_sdk_version()}",
                "- Node server versions: "
                f"filesystem={MCP_SERVER_FILESYSTEM_VERSION}; bash={BASH_MCP_VERSION}",
                f"- Degraded capabilities: {'optional MCP' if self.tool_provider_warnings else 'none recorded'}",
                f"- Actor started: {actor_started}",
                f"- Diff produced: {diff_produced}",
                f"- Verification executed: {verification_executed}",
                f"- Recovery: {self._recovery_action(category)}",
                f"- Run ID: {self.report_run_id or 'unknown'}",
                f"- Diagnostic log: {self.diagnostic_log_path or 'not recorded'}",
            ])
            if len(self.errors) > 1:
                lines.append(f"- Secondary errors: {len(self.errors) - 1}")
            lines.append("")

        if self.sandbox_backends:
            lines.extend([
                "## Command Sandbox",
                "",
                f"- Backends: {', '.join(sorted(self.sandbox_backends))}",
                f"- Isolated execution observed: {self.isolated_execution_observed}",
                "",
            ])

        lines.extend(["## Files", ""])

        if self.files_referenced:
            for path in sorted(self.files_referenced):
                lines.append(f"- {path}")
        else:
            lines.append("- No file paths were observed in tool arguments.")

        lines.extend(["", "## Tools", ""])
        if self.tool_calls:
            for call in self.tool_calls:
                status = "unknown"
                if call.success is True:
                    status = "ok"
                elif call.success is False:
                    status = "failed"
                lines.append(f"- {call.name}: {status}")
        else:
            lines.append("- No tool calls were observed.")

        lines.extend(["", "## Tests", ""])
        test_commands = self.observed_test_commands
        if test_commands:
            for command in test_commands:
                lines.append(f"- Observed test command: `{command}`")
        else:
            lines.append("- No test command was observed in this run.")

        lines.extend(["", "## Diagnostics", ""])
        counts = self._tool_name_counts()
        lines.extend([
            f"- Search/read calls: {counts['search_read']}",
            f"- Edit/write/patch calls: {counts['mutation']}",
            f"- Run calls: {counts['run']}",
            "- First edit before tool call: "
            + (
                str(self.first_edit_tool_call_index)
                if self.first_edit_tool_call_index is not None
                else "none"
            ),
        ])
        if self.actor_status_counts:
            status_text = ", ".join(
                f"{status}={count}"
                for status, count in sorted(self.actor_status_counts.items())
            )
            lines.append(f"- Actor statuses: {status_text}")
        if self.actor_failure_categories:
            category_text = ", ".join(
                f"{category}={count}"
                for category, count in sorted(self.actor_failure_categories.items())
            )
            lines.append(f"- Actor failure categories: {category_text}")
        if self.tool_provider_warnings:
            lines.append(f"- Tool provider warnings: {len(self.tool_provider_warnings)}")

        lines.extend(["", "## Risk", ""])
        assessed_risk = (
            str(self.task_assessment.get("risk", ""))
            if self.task_assessment
            else ""
        )
        if assessed_risk == "high":
            lines.append("- High: task assessment requires review or human approval.")
        elif assessed_risk == "medium":
            lines.append("- Medium: task assessment identified a cross-boundary change.")
        elif self.errors:
            lines.append("- Review required: runtime errors were observed.")
        elif self.failed_tool_count:
            lines.append("- Review required: at least one tool call failed.")
        else:
            lines.append("- Low: no runtime errors or failed tool calls were observed.")

        if self.total_tokens:
            lines.extend([
                "",
                "## Token Usage",
                "",
                f"- Prompt tokens: {self.prompt_tokens}",
                f"- Completion tokens: {self.completion_tokens}",
                f"- Total tokens: {self.total_tokens}",
                f"- Source: {'estimated' if self.usage_estimated else 'provider-reported'}",
            ])

        if self.guardrail_calls:
            lines.extend([
                "",
                "## Guardrail Usage (separate from agent tokens)",
                "",
                f"- Calls: {self.guardrail_calls}",
                f"- Prompt tokens: {self.guardrail_prompt_tokens}",
                f"- Completion tokens: {self.guardrail_completion_tokens}",
                f"- Total tokens: {self.guardrail_prompt_tokens + self.guardrail_completion_tokens}",
                f"- Failures: {self.guardrail_failures}",
                f"- Tripwires: {self.guardrail_tripwires}",
                f"- Total latency: {self.guardrail_latency_ms:.2f} ms",
            ])

        if self.final_output:
            lines.extend([
                "",
                "## Final Output",
                "",
                self.final_output.strip(),
            ])

        return "\n".join(lines).rstrip() + "\n"

    def write_final_report(
        self,
        workspace_dir: str | Path,
        run_id: str,
        *,
        state_dir: str | Path | None = None,
    ) -> Path:
        output_dir = (
            Path(state_dir)
            if state_dir is not None
            else touch_workspace_state(workspace_dir)
        )
        report_path = output_dir / "final_report.md"
        safe_run_id = safe_state_component(run_id, fallback="run")
        history_path = output_dir / "reports" / f"{safe_run_id}.md"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_run_id = safe_run_id
        self.diagnostic_log_path = str(output_dir / "logs" / f"{safe_run_id}.log")
        content = self.to_markdown()
        history_path.write_text(content, encoding="utf-8")
        report_path.write_text(content, encoding="utf-8")
        return report_path

    @staticmethod
    def _failure_category(error: str) -> str:
        lowered = error.lower()
        if "mcp" in lowered or "tool provider" in lowered:
            return "tool provider failure"
        if "verification" in lowered or "pytest" in lowered:
            return "verification failure"
        if "sandbox" in lowered or "worktree" in lowered or "bootstrap" in lowered:
            return "environment/bootstrap failure"
        if "policy" in lowered or "denied" in lowered:
            return "policy denial"
        return "runtime failure"

    def _primary_failure(self) -> str:
        if self.errors:
            return self.errors[0]
        return next(
            (
                str(redact_text(call.detail).value)
                for call in self.tool_calls
                if call.success is False and call.detail
            ),
            "",
        )

    @staticmethod
    def _provider_name(error: str) -> str:
        import re

        match = re.search(r"(?:server|provider)\s+['\"]?([\w.-]+)", error, re.I)
        return match.group(1) if match else "not identified"

    @staticmethod
    def _exception_type(error: str) -> str:
        import re

        match = re.search(r"\b([A-Za-z]+(?:Error|Exception))\b", error)
        return match.group(1) if match else "not recorded"

    @staticmethod
    def _recovery_action(category: str) -> str:
        if category == "tool provider failure":
            return "Use SCA_MCP_MODE=off or run `sca doctor`; require MCP only when explicitly needed."
        if category == "environment/bootstrap failure":
            return "Run `sca doctor` and repair the reported local sandbox or installation issue."
        if category == "verification failure":
            return "Inspect the verification artifact and fix the first failing quality gate."
        return "Inspect the primary error and diagnostic log before retrying."

    def _record_file_args(self, args: dict[str, Any]) -> None:
        for key in FILE_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                self.files_referenced.add(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        self.files_referenced.add(item)

    def _matching_tool_call(self, tool_name: str) -> ToolCallRecord:
        if not tool_name:
            return self.tool_calls[-1]
        for record in reversed(self.tool_calls):
            if record.name == tool_name and record.success is None:
                return record
        return self.tool_calls[-1]

    def _record_actor_update(self, content: str) -> None:
        try:
            snapshot = json.loads(content)
        except json.JSONDecodeError:
            return
        counts: dict[str, int] = {}
        categories: dict[str, int] = {}
        for task in snapshot.get("task_tree", {}).values():
            status = task.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
            category = task.get("failure_category")
            if isinstance(category, str) and category:
                categories[category] = categories.get(category, 0) + 1
        self.actor_status_counts = counts
        self.actor_failure_categories = categories

    def _tool_name_counts(self) -> dict[str, int]:
        search_read = {
            "list_dir",
            "list_directory",
            "read",
            "read_file",
            "read_text_file",
            "read_outline",
            "search_codebase",
            "search_files",
        }
        mutation = {"edit_file", "write_file", "apply_patch"}
        run = {"run", "bash", "run_background"}
        return {
            "search_read": sum(1 for call in self.tool_calls if call.name in search_read),
            "mutation": sum(1 for call in self.tool_calls if call.name in mutation),
            "run": sum(1 for call in self.tool_calls if call.name in run),
        }

    def _record_task_assessment(self, content: str) -> None:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return
        self.task_assessment = payload

    def _record_execution_policy(self, content: str) -> None:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return
        self.execution_policy = payload

    def _record_sandbox_execution(self, content: str) -> None:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        backend = payload.get("backend")
        if isinstance(backend, str) and backend:
            self.sandbox_backends.add(backend)
        self.isolated_execution_observed = (
            self.isolated_execution_observed or payload.get("isolated") is True
        )

    def _record_token_stats(self, event: AgentEvent) -> None:
        # Structured fields are preferred for new runtime events. JSON content
        # remains supported for traces created by older versions.
        if event.prompt_tokens or event.completion_tokens:
            self.prompt_tokens = event.prompt_tokens
            self.completion_tokens = event.completion_tokens
            self.usage_estimated = event.usage_estimated
            return
        try:
            stats = json.loads(event.content)
        except json.JSONDecodeError:
            return
        self.prompt_tokens = int(stats.get("prompt_tokens", 0) or 0)
        self.completion_tokens = int(stats.get("completion_tokens", 0) or 0)
        self.usage_estimated = bool(stats.get("estimated", False))

    def _record_model_usage(self, event: AgentEvent) -> None:
        """Accumulate per-call usage when a terminal token_stats event is absent."""
        if event.prompt_tokens or event.completion_tokens:
            self.prompt_tokens += event.prompt_tokens
            self.completion_tokens += event.completion_tokens
            self.usage_estimated = self.usage_estimated or event.usage_estimated
            return
        try:
            usage = json.loads(event.content or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(usage, dict):
            return
        self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        self.usage_estimated = self.usage_estimated or bool(
            usage.get("estimated", False)
        )

    def _record_guardrail_usage(self, event: AgentEvent) -> None:
        try:
            payload = json.loads(event.content or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict) or not payload.get("guardrail_called"):
            return
        self.guardrail_calls += 1
        self.guardrail_prompt_tokens += int(
            payload.get("guardrail_prompt_tokens", 0) or 0
        )
        self.guardrail_completion_tokens += int(
            payload.get("guardrail_completion_tokens", 0) or 0
        )
        self.guardrail_failures += int(bool(payload.get("sanitized_error")))
        self.guardrail_tripwires += int(bool(payload.get("guardrail_tripwire")))
        self.guardrail_latency_ms += float(
            payload.get("guardrail_latency_ms", 0.0) or 0.0
        )
