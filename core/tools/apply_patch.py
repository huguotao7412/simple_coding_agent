"""apply_patch tool — Planner-only tool for merging Actor diffs into the main workspace."""

from __future__ import annotations

import glob
import os
import subprocess
import tempfile

from .base import BaseTool, ToolResult
from ..git_utils import is_clean


def _cleanup_rej_files(base_dir: str) -> dict[str, str]:
    """Scan base_dir for *.rej files, read their contents, and delete them.

    Returns a dict mapping filename → content (first 500 chars each).
    Call this after any patch application attempt to keep the workspace clean.
    """
    rejected: dict[str, str] = {}
    pattern = os.path.join(base_dir, "**", "*.rej")
    for rej_path in glob.glob(pattern, recursive=True):
        try:
            with open(rej_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()[:500]
            rel_path = os.path.relpath(rej_path, base_dir)
            rejected[rel_path] = content
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
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = (
        "Apply a unified diff (produced by an Actor) to the main workspace. "
        "Use this to merge Actor changes back after a delegate call completes. "
        "If the patch conflicts, you will receive the conflict details and can "
        "spawn a dedicated Actor to resolve them.\n\n"
        "Strategy: 'strict' (default) fails on any conflict. "
        "'fuzz' applies what it can and writes .rej files for rejected hunks."
    )
    parameters = {
        "diff": {
            "type": "string",
            "description": "The unified diff string to apply. Copy from an Actor's returned diff field.",
        },
        "task_id": {
            "type": "string",
            "description": "The task_id that produced this diff (for tracking).",
        },
        "strategy": {
            "type": "string",
            "enum": ["strict", "fuzz"],
            "description": (
                "Merge strategy: 'strict' (fail on any conflict) or "
                "'fuzz' (apply partial, create .rej files). Default: 'strict'."
            ),
        },
    }
    required_params = ["diff", "task_id"]

    async def execute(
        self,
        diff: str,
        task_id: str,
        strategy: str = "strict",
        workspace_dir: str = "",
    ) -> ToolResult:
        if not diff or not diff.strip():
            return ToolResult.ok(f"No changes to apply for task {task_id} (empty diff).")

        base_dir = workspace_dir or os.getcwd()

        # --- Pre-check: workspace must be clean ---
        if not is_clean(base_dir):
            return ToolResult.fail(
                "Main workspace is dirty (uncommitted changes exist). "
                "Please commit or stash your changes before applying patches. "
                "This ensures patches apply cleanly and conflicts are traceable."
            )

        # --- Write diff to temp file ---
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
                newline="",
            ) as f:
                f.write(diff)
                patch_path = f.name
        except OSError as e:
            return ToolResult.fail(f"Failed to write patch file: {e}")

        try:
            # --- Dry-run: check if patch applies cleanly ---
            rc, stdout, stderr = _run_git(
                "apply", "--check", patch_path,
                cwd=base_dir, timeout=30,
            )
            if rc != 0:
                # Dry-run failed — try to identify conflicting files
                conflict_files = _parse_conflict_files(stderr)
                if strategy == "fuzz":
                    # Try with --reject to apply what we can
                    rc2, stdout2, stderr2 = _run_git(
                        "apply", "--reject", patch_path,
                        cwd=base_dir, timeout=30,
                    )
                    # Collect and clean up .rej files
                    rejected = _cleanup_rej_files(base_dir)

                    _run_git("add", "-A", cwd=base_dir, timeout=10)
                    _run_git("commit", "-m", f"Auto-merge partial patch for task {task_id}", cwd=base_dir, timeout=10)

                    result_parts = [
                        f"Patch for {task_id} partially applied (fuzz mode).",
                        f"Conflicts in: {', '.join(conflict_files) if conflict_files else 'unknown files'}.",
                    ]
                    if rc2 != 0:
                        result_parts.append(f"Partial application output: {stderr2}")
                    if rejected:
                        result_parts.append(
                            f"\nRejected hunks ({len(rejected)} .rej files cleaned up):"
                        )
                        for fname, content in rejected.items():
                            result_parts.append(f"\n--- {fname} ---\n{content}")
                    else:
                        result_parts.append(
                            "\nNo .rej files generated — all hunks applied cleanly."
                        )
                    return ToolResult.ok("\n".join(result_parts))
                else:
                    return ToolResult.fail(
                        f"Patch for {task_id} conflicts with current workspace state.\n"
                        f"Conflicting files: {', '.join(conflict_files) if conflict_files else 'unknown'}.\n"
                        f"Git error details:\n{stderr}\n\n"
                        f"=== RESOLUTION PROTOCOL ===\n"
                        f"1. Create a new task via update_state: 'Resolve merge conflict for {task_id}'\n"
                        f"2. Delegate this task to a single Actor. Inject as context_files the\n"
                        f"   conflicting file paths listed above, plus the original diff below.\n"
                        f"3. The resolution Actor should read the conflicting files, understand\n"
                        f"   both the current state AND the intended changes, manually merge,\n"
                        f"   and produce a clean unified diff as output.\n"
                        f"4. Apply the resolution Actor's clean diff with apply_patch.\n"
                        f"5. If resolution also fails, retry ONCE with strategy='fuzz'.\n"
                        f"6. After 2 failed resolution attempts, report to the user.\n"
                        f"=== ORIGINAL DIFF (first 2000 chars) ===\n"
                        f"{diff[:2000]}"
                    )

            # --- Apply the patch ---
            rc, stdout, stderr = _run_git(
                "apply", patch_path,
                cwd=base_dir, timeout=30,
            )
            if rc == 0:

                _run_git("add", "-A", cwd=base_dir, timeout=10)
                _run_git("commit", "-m", f"Merge Actor changes for task {task_id}", cwd=base_dir, timeout=10)

                return ToolResult.ok(
                    f"Patch for {task_id} applied successfully to main workspace."
                )
            else:
                return ToolResult.fail(f"git apply failed for {task_id}: {stderr}")

        finally:
            # Clean up temp patch file
            try:
                os.unlink(patch_path)
            except OSError:
                pass
            # Clean up any stray .rej files from previous runs
            _cleanup_rej_files(base_dir)


def _parse_conflict_files(git_stderr: str) -> list[str]:
    """Extract conflicting file paths from git apply error output.

    Git error formats:
      error: patch failed: path/to/file: <reason>
      error: path/to/file: <reason>
    """
    files: list[str] = []
    for line in git_stderr.split("\n"):
        line = line.strip()
        if not line.startswith("error:"):
            continue
        # Remove the "error:" prefix
        rest = line[len("error:"):].strip()
        if rest.startswith("patch failed:"):
            rest = rest[len("patch failed:"):].strip()
        # Now rest should be "path/to/file: <reason>"
        if ":" in rest:
            fname = rest.split(":", 1)[0].strip()
        else:
            fname = rest.strip()
        if fname and fname != "patch failed":
            files.append(fname)
    return files if files else []
