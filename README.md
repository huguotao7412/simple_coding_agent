# Simple Coding Agent

Simple Coding Agent is a **CLI-first local coding agent runtime** focused on transparent tool use, isolated execution, and verifiable code changes.

The project is intentionally aimed at developers who work in terminals, Git repositories, and test suites. The Web UI still exists, but it is experimental and not the primary product surface.

## What This Project Demonstrates

- A shared ReAct-style agent runtime for Planner and Actor agents.
- One correlated event stream for Planner and nested Actor thoughts, tool calls, results, errors, usage, and task updates.
- Centralized tool-call parsing with malformed JSON recovery.
- Runtime safety controls such as max-step limits and repeated-action circuit breaking.
- Run-scoped task state and correlation IDs, without process-global Planner state.
- Planner/Actor orchestration with execution-time role tool authorization.
- Versioned A2A_lite messages with automatic structured dependency handoffs.
- Typed artifact references with integrity digests for Actor patches.
- MCP-backed file and shell tools for isolated Actor execution.
- Git worktree isolation for delegated Actor tasks.
- Deterministic project quality gates with bounded automatic repair.
- Full Actor patch artifacts under `.sca/artifacts/actor-diffs/`.
- Persistent JSONL traces for eval/debug runs.
- Durable checkpoints for listing, inspecting, and resuming non-interactive runs.
- Local eval runner with aggregate `eval_results.json` metrics.
- Versioned deterministic task assessment with auditable strategy recommendations.
- Versioned runtime-enforced execution policies for Actor topology, model calls, tokens, failed tools, repairs, and active wall time.
- Fail-closed high-risk approval plus durable policy and budget consumption in checkpoints.
- Replaceable local/E2B sandbox protocol shared by Actor shell and verification.
- Safety eval fixtures for path escape, dirty workspace, and destructive command behavior.
- Deterministic unit tests for runtime, isolation, reports, and eval behavior.

This is not positioned as a fully autonomous production coding system yet. The current goal is a reliable, auditable local agent core that can be improved and measured over time.

## Current Status

Primary interface:

- `sca` - CLI coding agent REPL.

Dashboard interface:

- `sca-web` - Streamlit Trace/Eval Dashboard for inspecting eval results, traces, reports, and Actor patch artifacts. A legacy Live Agent view is still available from the sidebar.

## Architecture

```text
User request
  -> TaskAssessor (intent / complexity / risk / strategy)
  -> ExecutionPolicy / RunBudgetLedger
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
- `ExecutionPolicy` compiles assessment output into topology and resource limits that tool calls cannot override; one `RunBudgetLedger` is shared by Planner and all Actors.
- `GlobalState` records task state and Actor updates.

## Project Layout

```text
core/
  a2a_lite/         versioned messages, structured handoffs, artifact references
  runtime/          execution engine and conversation context
  runs/             durable run lifecycle, task state, persistence adapters
  actors/           Actor behavior, contracts, roles, worktree adapter
  verification/     Quality-gate config, execution evidence, repair prompts
  execution/        Deterministic task assessment and strategy contracts
  sandbox/          Command sandbox protocol, E2B adapter, and workspace transport
  planner.py        Planner wrapper around the runtime engine
  events.py         cross-domain AgentEvent protocol
  llm.py            OpenAI-compatible async streaming client
  mcp/              MCP tool provider
  tools/            local planner/actor tools

cli/
  main.py           CLI entrypoint
  bridge.py         runtime event -> terminal UI bridge
  ui.py             Rich terminal renderer

web/
  Streamlit Trace/Eval Dashboard and optional Live Agent panel

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
SCA_SANDBOX_BACKEND=e2b
E2B_API_KEY=e2b_your_key_here
SCA_E2B_TEMPLATE=base
SCA_E2B_ALLOW_INTERNET=false
SCA_SANDBOX_MAX_TIMEOUT=300
SCA_SANDBOX_MAX_TRANSFER=50000000
```

The client uses an OpenAI-compatible chat completions API.

### Command sandbox

`local` is the compatibility default and is **not OS-isolated**. Check the active
backend with:

```powershell
sca sandbox-check
```

To enable the zero-local-runtime E2B backend, create an account and API key at
<https://e2b.dev/dashboard>, then set:

```powershell
$env:SCA_SANDBOX_BACKEND = "e2b"
$env:E2B_API_KEY = "e2b_your_key"
sca sandbox-check
```

E2B mode keeps Git/worktree lifecycle on the trusted host and executes shell and
verification commands in a remote Linux sandbox. A bounded archive transport syncs
the Actor worktree before and after each command. It rejects path traversal and never
uploads `.env`, Git metadata, virtual environments, package caches, or common
credential files. Internet access is blocked by default and must be explicitly
enabled for dependency installation. Selecting E2B fails closed when its SDK or API
key is unavailable; it never falls back to host execution. Remote execution sends
eligible repository files to E2B, so operators must account for source-code privacy.

Optionally define deterministic coder quality gates in `.sca/quality-gates.toml`:

```toml
max_repair_attempts = 2

[[gates]]
name = "unit"
command = ["{python}", "-m", "pytest", "-q"]
timeout_seconds = 120

[[gates]]
name = "types"
command = ["{python}", "-m", "mypy", "core"]
required = false
```

Commands are argument arrays and run without a shell in the coder's isolated worktree. Required failures are returned to the same Actor for bounded repair; a diff is exported only after all required gates pass. Complete output is retained under `.sca/artifacts/verification/`. These are repository-owned commands and execute with the current user's permissions, so only enable configurations you trust.

## Usage

Run the CLI in the current repository:

```bash
sca
```

Run against another workspace:

```bash
sca --dir C:\path\to\project
```

Run a resumable non-interactive task:

```bash
sca --dir C:\path\to\project --prompt "Fix the failing tests"
```

Tasks deterministically classified as high risk stop before the first model call. After reviewing the intended side effects, explicitly approve a new or resumed run:

```bash
sca --approve-high-risk --dir C:\path\to\project --prompt "Run the reviewed database migration"
sca --approve-high-risk --dir C:\path\to\project resume run_abc123
```

This flag is a local CLI authorization signal, not a multi-party approval service. It does not bypass tool allowlists, command guards, workspace boundaries, or the sandbox.

Inspect and resume local runs:

```bash
sca --dir C:\path\to\project runs
sca --dir C:\path\to\project inspect run_abc123
sca --dir C:\path\to\project resume run_abc123
```

Checkpoints are stored in `<workspace>/.sca/runs.db`. `runs` and `inspect` are read-only and do not require a model API key. P1 recovery covers single-task `--prompt` runs; the multi-turn interactive REPL remains an in-memory session for now.

Experimental Web UI:

```bash
sca-web
```

By default this opens the Trace/Eval Dashboard. Point it at an `eval_results.json` file to inspect pass rate, duration, tool calls, token usage, per-task timelines, final reports, failures, and Actor patch artifacts.

## CLI Event Transparency

The CLI renders the runtime event stream:

- deterministic task assessment and recommended execution strategy
- streamed model output
- tool call names and compact arguments
- tool success/failure summaries
- Actor task updates
- context compaction notices
- whole-run token usage summaries, labeled as provider-reported or estimated
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
- destructive Actor shell commands such as recursive delete and hard reset are blocked before reaching bash MCP
- E2B mode routes Actor shell and deterministic verification through one fail-closed remote backend
- Actor role allowlists are enforced again immediately before local or MCP tool dispatch
- committed root tool-call results are reused during recovery instead of being executed again

Git worktrees provide version-control and working-directory separation, not an operating-system sandbox. Shell processes still run with the permissions of the current user, so only run the agent against repositories and environments you are willing to modify.

SQLite cannot atomically commit an arbitrary shell, filesystem, or network side effect together with its tool-result checkpoint. A crash after the side effect but before the checkpoint can still cause a retry. See [ADR-0002](docs/adr/0002-durable-run-store.md) for the exact transaction boundary.

## Development

Run all tests:

```bash
.\.venv\Scripts\python.exe -m pytest
```

Type-check the trusted runtime boundary:

```bash
.\.venv\Scripts\python.exe -m mypy core/execution core/sandbox core/actors/contracts.py core/policy.py core/events.py core/runs/models.py core/runs/store.py core/runs/sqlite_store.py core/runs/context.py core/runtime/engine.py core/actors/worktree.py core/verification core/planner.py core/actors/agent.py core/mcp/client.py core/tools/update_state.py core/tools/delegate.py core/tools/sandbox_run.py cli/report.py cli/runs.py cli/main.py evals/run_evals.py
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

The aggregate results include per-task assessments and strategy counts. The comparison
report shows pass rate, duration, tool calls, failed tools, token usage, and task-level
regressions/improvements against the first file as baseline.

The same artifacts can be inspected visually with:

```bash
sca-web
```

## Roadmap

Near-term:

- Calibrate task-assessment rules against repeated real-model eval baselines.
- Expand multi-file, recovery, conflict, and fault-injection evals and compare cost per success.

Longer-term:

- Better merge/conflict workflows for Actor diffs.
- More robust model/provider routing.
- Auditable interactive or multi-party approval workflows for destructive/high-risk operations.
