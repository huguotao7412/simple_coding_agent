# Simple Coding Agent

Simple Coding Agent is a **CLI-first local coding agent runtime** focused on transparent tool use, isolated execution, and verifiable code changes.

The project is intentionally aimed at developers who work in terminals, Git repositories, and test suites. The Web UI still exists, but it is experimental and not the primary product surface.

## What This Project Demonstrates

- A shared ReAct-style agent runtime for Planner and Actor agents.
- Transparent streaming events for thoughts, tool calls, tool results, errors, token usage, and task updates.
- Centralized tool-call parsing with malformed JSON recovery.
- Runtime safety controls such as max-step limits and repeated-action circuit breaking.
- Planner/Actor orchestration with role-specific tool access.
- MCP-backed file and shell tools for isolated Actor execution.
- Git worktree isolation for delegated Actor tasks.
- Full Actor patch artifacts under `.sca/artifacts/actor-diffs/`.
- Persistent JSONL traces for eval/debug runs.
- Local eval runner with aggregate `eval_results.json` metrics.
- Deterministic unit tests for runtime, isolation, reports, and eval behavior.

This is not positioned as a fully autonomous production coding system yet. The current goal is a reliable, auditable local agent core that can be improved and measured over time.

## Current Status

Primary interface:

- `sca` - CLI coding agent REPL.

Experimental interface:

- `sca-web` - Streamlit visual panel. Useful for future visualization work, but not part of the core milestone.

## Architecture

```text
User request
  -> Planner
  -> AgentRuntime
  -> LLM response
  -> tool-call parser
  -> tool executor
  -> context observation
  -> transparent event stream
  -> CLI renderer
```

The codebase keeps the Planner/Actor split:

- `Planner` decides how to break down work and can delegate subtasks.
- `ActorAgent` executes isolated subtasks with role-specific permissions.
- `AgentRuntime` owns the shared loop: LLM calls, tool parsing, tool execution, context compaction, step limits, repeated-action detection, and event emission.
- `GlobalState` records task state and Actor updates.

## Project Layout

```text
core/
  runtime.py        shared ReAct runtime and AgentEvent protocol
  planner.py        Planner wrapper around AgentRuntime
  agent.py          ActorAgent wrapper around AgentRuntime
  context.py        conversation and context compression
  llm.py            OpenAI-compatible async streaming client
  state.py          task ledger and state snapshots
  mcp/              MCP tool provider
  tools/            local planner/actor tools

cli/
  main.py           CLI entrypoint
  bridge.py         runtime event -> terminal UI bridge
  ui.py             Rich terminal renderer

web/
  experimental Streamlit UI

tests/
  test_runtime.py          deterministic runtime tests
  test_cli_report.py       final report audit tests
  test_delegate_baseline.py dependency diff baseline tests
  test_evals.py            local eval runner tests
  test_mcp_provider.py     MCP provider isolation tests
```

See [architecture.md](architecture.md) and [architecture_CN.md](architecture_CN.md) for the Planner/Actor lifecycle, worktree isolation, MCP boundary, and eval design.

## Installation

Requires Python 3.12+ and Node.js 18+ if you want MCP Actor tools.

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
npm install
```

For the experimental Web UI:

```bash
pip install -e ".[web]"
```

## Configuration

Create `.env` in the repository root:

```bash
SCA_API_KEY=your-api-key
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_MAX_ACTORS=4
```

The client uses an OpenAI-compatible chat completions API.

## Usage

Run the CLI in the current repository:

```bash
sca
```

Run against another workspace:

```bash
sca --dir C:\path\to\project
```

Experimental Web UI:

```bash
sca-web
```

## CLI Event Transparency

The CLI renders the runtime event stream:

- streamed model output
- tool call names and compact arguments
- tool success/failure summaries
- Actor task updates
- context compaction notices
- token usage summaries
- errors and final output

The goal is to make each run inspectable instead of treating the agent as a black box.

## Safety Model

The current safety model is pragmatic rather than magical:

- tool calls are parsed centrally and malformed JSON is handled as recoverable feedback
- repeated identical tool calls are circuit-broken
- max-step limits prevent unbounded loops
- file operations validate workspace boundaries in local tools and MCP providers
- delegated Actor tasks use isolated git worktrees
- dependent Actor tasks receive upstream diffs as a committed worktree baseline
- full Actor diffs are persisted as patch artifacts, while Planner context receives a compact preview
- MCP server packages are pinned and launched from local `node_modules/.bin` when installed
- Planner and Actor roles can receive different tool allowlists

## Development

Run all tests:

```bash
.\.venv\Scripts\python.exe -m pytest
```

Compile-check Python modules:

```bash
.\.venv\Scripts\python.exe -m compileall core cli web tests
```

CLI smoke check:

```bash
.\.venv\Scripts\python.exe -m cli.main --help
```

Prepare local eval task workspaces:

```bash
sca-eval prepare
```

Run the full measurable eval loop against every fixture:

```bash
sca-eval run --model deepseek-v4-pro
```

This writes:

- `eval_results.json` at the repository root by default
- `.sca/final_report.md` inside each candidate task workspace
- `.sca/traces/run_trace.jsonl` inside each candidate task workspace
- `.sca/artifacts/actor-diffs/*.patch` for full Actor-produced diffs

You can still run tasks manually with `sca --dir tmp/eval-runs/<task_id>`. When finished, check the results:

```bash
sca-eval check
```

Compare two or more aggregate eval runs:

```bash
sca-eval compare eval_results.baseline.json eval_results.candidate.json --output eval_comparison.md
```

The comparison report shows pass rate, duration, tool calls, failed tools, token usage, and task-level regressions/improvements against the first file as baseline.

## Roadmap

Near-term:

- Add safety-focused eval cases for path escape, destructive commands, and dirty workspaces.
- Keep Web UI experimental unless it becomes useful for trace visualization.

Longer-term:

- Better merge/conflict workflows for Actor diffs.
- More robust model/provider routing.
- Human approval gates for destructive or high-risk operations.
