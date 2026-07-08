# Local Eval Suite

This directory contains deterministic benchmark tasks for Simple Coding Agent.

The suite can run the real agent, but the check phase is offline and deterministic:
it validates copied fixture workspaces by file changes, required content, forbidden
paths, final reports, and test commands.

## Task Registry

Tasks are defined in `evals/tasks.json`. Fixture projects live under
`evals/fixtures/<task_id>/`.

Current tasks:

- `fix_failing_pytest` - fix a failing unit test without changing the test.
- `add_type_hints` - add public function annotations while preserving behavior.
- `refactor_small_module` - refactor duplicated subtotal logic.
- `add_cli_argument` - add a `--shout` CLI flag.
- `update_readme_and_test` - update README documentation and its synchronized test.
- `path_escape_guard` - reject a parent-directory path escape request.
- `dirty_workspace_guard` - preserve seeded uncommitted user changes.
- `destructive_command_guard` - refuse destructive shell-command deletion.

## Usage

Copy fresh fixture workspaces:

```bash
sca-eval prepare
```

Run the full measurable agent loop:

```bash
sca-eval run --model deepseek-v4-pro
```

This writes:

- `eval_results.json` at the repository root by default
- `.sca/final_report.md` in each task workspace
- `.sca/traces/run_trace.jsonl` in each task workspace
- `.sca/artifacts/actor-diffs/*.patch` for full Actor-produced diffs

Check completed candidate workspaces:

```bash
sca-eval check
```

Compare aggregate result files:

```bash
sca-eval compare eval_results.baseline.json eval_results.candidate.json --output eval_comparison.md
```

The expected candidate layout is:

```text
tmp/eval-runs/
  fix_failing_pytest/
  add_type_hints/
  refactor_small_module/
  add_cli_argument/
  update_readme_and_test/
  path_escape_guard/
  dirty_workspace_guard/
  destructive_command_guard/
```
