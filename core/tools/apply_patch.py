"""Planner-only tool for merging Actor diffs into the main workspace."""

from __future__ import annotations

import glob
import os
import subprocess
import tempfile

from .base import BaseTool, ToolResult
from ..git_utils import is_clean
from ..runs.context import RunContext
from ..runs.task_state import GlobalState


def _cleanup_rej_files(base_dir: str) -> dict[str, str]:
    """Read and delete any *.rej files produced by a patch attempt."""
    rejected: dict[str, str] = {}
    pattern = os.path.join(base_dir, "**", "*.rej")
    for rej_path in glob.glob(pattern, recursive=True):
        try:
            with open(rej_path, encoding="utf-8", errors="replace") as f:
                content = f.read()[:500]
            rejected[os.path.relpath(rej_path, base_dir)] = content
        except OSError:
            pass
        try:
            os.unlink(rej_path)
        except OSError:
            pass
    return rejected


def _run_git(*args: str, cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = (
        "Apply a unified diff produced by an Actor to the main workspace. "
        "Use this to merge Actor changes back after a delegate call completes. "
        "The tool applies changes to the working tree but does not commit them. "
        "Strategy: 'strict' (default) fails on any conflict. "
        "'fuzz' applies what it can and reports rejected hunks."
    )
    parameters = {
        "diff": {
            "type": "string",
            "description": "The unified diff string to apply.",
        },
        "task_id": {
            "type": "string",
            "description": "The task_id that produced this diff.",
        },
        "strategy": {
            "type": "string",
            "enum": ["strict", "fuzz"],
            "description": "Merge strategy. Default: strict.",
        },
    }
    required_params = ["diff", "task_id"]

    def __init__(
        self,
        state: GlobalState | None = None,
        run_context: RunContext | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._run_context = run_context

    def _authorize_patch(self, task_id: str, diff: str) -> str:
        run_context = self._run_context
        if run_context is None or run_context.execution_policy is None:
            return ""
        state = self._state or run_context.state
        node = state.task_tree.get(task_id)
        if node is None:
            return f"Patch provenance denied: unknown task_id '{task_id}'"
        if node.status != "done":
            return f"Patch provenance denied: task '{task_id}' is not done"
        if node.actor_role != "coder":
            return f"Patch provenance denied: task '{task_id}' was not a Coder"
        if (node.diff or "") != diff:
            return f"Patch provenance denied: diff does not match task '{task_id}'"
        if (
            run_context.execution_policy.require_quality_gates
            and node.verification_passed is not True
        ):
            return (
                "Patch provenance denied: required quality gates did not produce "
                "passing evidence"
            )
        return ""

    async def execute(  # type: ignore[override]
        self,
        diff: str,
        task_id: str,
        strategy: str = "strict",
        workspace_dir: str = "",
    ) -> ToolResult:
        denial = self._authorize_patch(task_id, diff)
        if denial:
            return ToolResult.deny(denial)
        if not diff or not diff.strip():
            return ToolResult.ok(f"No changes to apply for task {task_id} (empty diff).")

        base_dir = workspace_dir or os.getcwd()
        if not is_clean(base_dir):
            return ToolResult.fail(
                "Main workspace is dirty (uncommitted changes exist). "
                "Commit or stash current changes before applying Actor patches."
            )

        patch_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
                newline="",
            ) as patch_file:
                patch_file.write(diff)
                patch_path = patch_file.name
        except OSError as e:
            return ToolResult.fail(f"Failed to write patch file: {e}")

        try:
            rc, _, stderr = _run_git("apply", "--check", patch_path, cwd=base_dir)
            if rc != 0:
                conflict_files = _parse_conflict_files(stderr)
                if strategy != "fuzz":
                    return ToolResult.fail(
                        f"Patch for {task_id} conflicts with current workspace state.\n"
                        f"Conflicting files: {', '.join(conflict_files) if conflict_files else 'unknown'}.\n"
                        f"Git error details:\n{stderr}\n\n"
                        f"Resolution protocol:\n"
                        f"1. Create a task via update_state: 'Resolve merge conflict for {task_id}'.\n"
                        f"2. Delegate a single Actor with the conflicting files and original diff as context.\n"
                        f"3. Apply the resolution Actor's clean diff with apply_patch.\n"
                        f"4. If resolution also fails, retry once with strategy='fuzz'.\n"
                        f"Original diff preview:\n{diff[:2000]}"
                    )

                rc2, _, stderr2 = _run_git("apply", "--reject", patch_path, cwd=base_dir)
                rejected = _cleanup_rej_files(base_dir)
                result_parts = [
                    f"Patch for {task_id} partially applied (fuzz mode).",
                    f"Conflicts in: {', '.join(conflict_files) if conflict_files else 'unknown files'}.",
                    "Changes are left uncommitted for review.",
                ]
                if rc2 != 0:
                    result_parts.append(f"Partial application output: {stderr2}")
                if rejected:
                    result_parts.append(f"Rejected hunks ({len(rejected)} files cleaned up):")
                    for filename, content in rejected.items():
                        result_parts.append(f"\n--- {filename} ---\n{content}")
                else:
                    result_parts.append("No .rej files were generated.")
                return ToolResult.ok("\n".join(result_parts))

            rc, _, stderr = _run_git("apply", patch_path, cwd=base_dir)
            if rc != 0:
                return ToolResult.fail(f"git apply failed for {task_id}: {stderr}")

            return ToolResult.ok(
                f"Patch for {task_id} applied successfully. "
                "Changes are left uncommitted for review."
            )
        finally:
            if patch_path:
                try:
                    os.unlink(patch_path)
                except OSError:
                    pass
            _cleanup_rej_files(base_dir)


def _parse_conflict_files(git_stderr: str) -> list[str]:
    """Extract conflicting file paths from git apply error output."""
    files: list[str] = []
    for line in git_stderr.split("\n"):
        line = line.strip()
        if not line.startswith("error:"):
            continue
        rest = line[len("error:"):].strip()
        if rest.startswith("patch failed:"):
            rest = rest[len("patch failed:"):].strip()
        if ":" in rest:
            filename = rest.split(":", 1)[0].strip()
        else:
            filename = rest.strip()
        if filename and filename != "patch failed":
            files.append(filename)
    return files
