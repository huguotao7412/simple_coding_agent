# Eval System Design

## Goal

Build the first measurable feedback loop for Simple Coding Agent. The initial
system evaluates real coding tasks by copying fixture repositories, optionally
running the agent, executing verification commands, and writing machine-readable
and human-readable reports.

## Scope

The first iteration implements a fixture-based end-to-end runner. It does not
judge solution quality with an LLM and does not require the agent CLI contract to
be final. Agent execution is pluggable through an optional command template.

## Case Layout

Each eval case lives under `evals/cases/<case_name>/`:

```text
evals/cases/<case_name>/
  eval.json
  repo/
    ...
```

`eval.json` fields:

- `name`: stable case name.
- `prompt`: user task sent to the agent.
- `verification_commands`: commands run after agent execution.
- `expected_files`: optional list of files expected to change or exist.
- `timeout_seconds`: per-command timeout.

## Runner Behavior

`python -m evals.run_evals` discovers all cases unless `--case` is provided.
For each case, it copies `repo/` to an isolated temporary workspace, optionally
runs `--agent-command`, runs verification commands, records changed files, and
marks the case as passed only when verification succeeds.

The agent command is a string template. It may use:

- `{workspace}` for the temporary workspace path.
- `{prompt}` for the case prompt.

`--dry-run` skips agent execution and verification while still validating case
discovery and report writing.

## Reports

The runner writes:

- `evals/reports/latest.json`
- `evals/reports/latest.md`

First-version metrics:

- case pass/fail
- verification pass/fail
- duration seconds
- commands run
- stdout/stderr summaries
- changed files

## Extension Points

Future iterations can add Planner-only evals, tool-layer evals, LLM-as-judge
rubrics, prompt version tracking, token/cost tracing, and dashboards. The case
format should stay backward-compatible as those fields are added.
