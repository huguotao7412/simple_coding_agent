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
- Node-free baseline file/search/edit/run tools for Actor execution, with MCP as an optional enhancement.
- Git worktree isolation for delegated Actor tasks.
- Deterministic project quality gates with bounded automatic repair.
- Full Actor patch artifacts in the per-user SCA state directory, outside the target workspace.
- Persistent JSONL traces for eval/debug runs.
- Durable checkpoints for listing, inspecting, and resuming non-interactive runs.
- Local eval runner with aggregate `eval_results.json` metrics.
- Versioned deterministic task assessment with auditable strategy recommendations.
- Versioned runtime-enforced execution policies for Actor topology, model calls, tokens, failed tools, repairs, and active wall time.
- Fail-closed high-risk approval plus durable policy and budget consumption in checkpoints.
- Replaceable local/E2B sandbox protocol shared by Actor shell and verification.
- Safety eval fixtures for path escape, dirty workspace, and destructive command behavior.
- Deterministic unit tests for runtime, isolation, reports, and eval behavior.
- A single LangGraph 1.x durable control plane with async SQLite checkpoints,
  structured Actor DAG fan-out, persistent human approval, and the existing
  secure runtime as its data plane.

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

Low-risk bug fixes generally go straight to a Coder, which can search, read,
edit, and test in one loop. Scout remains available for genuinely broad work,
but it is intentionally short and read-only so evaluation traces do not spend
the whole budget on analysis without mutation.

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

## Hybrid security

SCA separates three boundaries:

1. OpenAI Guardrails Python is an optional probabilistic content-risk signal.
2. `core.security.SecurityMiddleware` is the deterministic PDP/PEP for
   capabilities, final-argument authorization, workspace boundaries, approval,
   destructive-command denial, redaction, and audit.
3. `SandboxBackend`, the OS, proxy, firewall, or E2B owns real process/network
   isolation. A worktree or URL text detector is not a sandbox.

External `ALLOW` never overrides local `DENY`. Unknown tools/capabilities are
denied, and every tool is reauthorized with its final name and arguments directly
before dispatch. Input approval never approves later tools.

Install the preview integration only when needed:

```bash
pip install -e ".[guardrails]"
```

The Python 3.12 compatibility range is
`openai-guardrails>=0.2.1,<0.3`. Third-party types stay behind
`core.security.guards`; SCA retains its existing streaming LLM client and calls
the official runtime directly with `check_plain_text(...,
suppress_tripwire=True, raise_guardrail_errors=True)`.

| Mode | Local content guard | External guard | External unavailable |
|---|---:|---:|---|
| `local` | on | off | unaffected |
| `hybrid` | on | configured only | structured warning/review; local limits remain |
| `strict` | on | required | fail closed |
| `off` | off | off | tool policy, approval, audit, sandbox, workspace and destructive-action controls stay on |

Use a dedicated key and trusted absolute configuration path:

```dotenv
SCA_SECURITY_MODE=hybrid
SCA_GUARDRAILS_CONFIG=C:\Users\me\.config\sca\guardrails-coding.json
SCA_GUARDRAILS_API_KEY=your-dedicated-key
SCA_GUARDRAILS_BASE_URL=https://api.openai.com/v1
SCA_GUARDRAILS_TIMEOUT=10
SCA_GUARDRAILS_MAX_CONCURRENCY=4
```

`SCA_API_KEY` and `E2B_API_KEY` are never reused. Recognized credentials are
removed from MCP, Actor, sandbox, and verification subprocess environments.
Copy [`examples/openai-guardrails-coding.json`](examples/openai-guardrails-coding.json)
to a user-controlled location; repository Guardrails config is not trusted or
loaded automatically.

External egress is allowlisted by stage, classification, host, and payload size
after local redaction. Source code, binary data, secrets, credentials, and raw
tool output are denied by default. URL detection, tool network authorization,
and real network blocking are three separate controls.

Guardrail calls, prompt/completion tokens, failures, tripwires, and latency are
tracked separately from agent usage. Timeout, concurrency, per-run call/token
budgets, and a consecutive-failure circuit breaker bound cost and latency.

## User Installation

Use `pipx` for a user-level CLI. It gives SCA an isolated Python environment and exposes `sca` on the user `PATH`, so neither the source checkout nor a target project's `.venv` needs to be activated.

Prepare `pipx` on Windows:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Restart the terminal, then install the current GitHub version:

```powershell
pipx install git+https://github.com/huguotao7412/simple_coding_agent.git
sca config init
sca config path
```

Edit the user config printed by `sca config path` and set `SCA_API_KEY`. The CLI can then be launched from any project directory; the current directory is the default workspace. Clean Git repositories use native worktrees, while non-Git or dirty Git directories use a temporary shadow repository without modifying the project's Git metadata:

```powershell
cd C:\path\to\any-project
sca
```

Upgrade or uninstall it with:

```powershell
pipx upgrade simple-coding-agent
pipx uninstall simple-coding-agent
```

Node.js 18+ is optional for enhanced MCP Actor tools. The wheel includes baseline Python tools for `list_dir`, `search_codebase`, `read`, `edit_file`, `write_file`, and `run`, so basic coding capability does not depend on `npm`, `npx`, or `node_modules`.

## Development Installation

Requires Python 3.12+. Install Node.js 18+ only if you want optional MCP Actor tool servers during development.

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

The user-level default is created by `sca config init`. A target workspace may also contain `.env` for project-specific overrides:

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

Precedence is process environment > current workspace `.env` > user config. SCA reads only the exact workspace `.env` and does not search parent directories, avoiding accidental configuration inheritance from the terminal launch location.

The client uses an OpenAI-compatible chat completions API.

### Durable orchestration

LangGraph is the only top-level control plane for interactive CLI,
non-interactive CLI, the Web Live Agent, local eval, and Harbor runs:

```powershell
sca
sca --prompt "Explain this repository"
sca --prompt "Fix the reviewed issue"
```

LangGraph checkpoints compact workflow state in the workspace-keyed user state
directory and use `run_id` as `thread_id`. High-risk runs pause with a structured
interrupt and resume with the existing `--approve-high-risk` flag. LangGraph
provides durable orchestration, not a security sandbox, and checkpoint replay is
not exactly-once execution for shell, filesystem, or network side effects. See
[ADR-0006](docs/adr/0006-langgraph-control-plane.md).

In the interactive CLI, every user request receives a separate durable Run and
LangGraph thread. A compact user/assistant history is carried into the next task,
while execution policy, tool results, and task DAG state remain run-scoped.
High-risk interrupts are rendered as an approval prompt and resume the same thread.

Runs created before LangGraph checkpoints remain available to `runs` and
`inspect`, but `resume` rejects them with an explicit migration explanation.
SCA never guesses a graph program counter. Start a new Run to continue that work.

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
command = ["{python}", "-m", "mypy"]
required = false
```

Commands are argument arrays and run without a shell in the coder's isolated worktree. Required failures are returned to the same Actor for bounded repair; a diff is exported only after all required gates pass. Complete output is retained in the per-user SCA state directory. These are repository-owned commands and execute with the current user's permissions, so only enable configurations you trust.

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

Checkpoints, reports, Actor patches, and verification logs are stored under a workspace-keyed directory in `%LOCALAPPDATA%\\sca\\workspaces` on Windows or `$XDG_STATE_HOME/sca/workspaces` on Unix. Set `SCA_STATE_HOME` to override this root. Target workspaces are not populated with runtime reports or databases; `.sca/quality-gates.toml` remains the optional project-owned configuration. `runs` and `inspect` are read-only and do not require a model API key.

Each workspace state directory contains `workspace.json` with the original
workspace path, creation time, last-access time, and optional orphan timestamp.
`final_report.md` always contains the latest report, while `reports/<run-id>.md`
retains run history. Lifecycle defaults keep every run from the last 30 days plus
the newest 50 runs, and cap total Actor/verification artifacts at 1 GiB. Override
these with `SCA_RETENTION_DAYS`, `SCA_RETAIN_RUNS`, and
`SCA_ARTIFACT_MAX_BYTES`.

```powershell
sca gc --dry-run
sca gc
sca runs delete <run-id>
```

GC is conservative for missing workspaces: the first real GC marks the state as
orphaned, and a later GC removes it only after the retention period. `--dry-run`
does not mutate state. Artifact capacity is enforced globally, oldest files first.
Active or paused durable runs are never removed by age/count retention.

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
- delegated Actor tasks use isolated Git worktrees; non-Git and dirty Git workspaces are snapshotted into an ephemeral shadow repository
- shadow-workspace merges compare touched-file hashes and refuse to overwrite concurrent user changes
- dependent Actor tasks receive upstream diffs as a committed worktree baseline
- full Actor diffs are persisted as patch artifacts, while Planner context receives a compact preview
- MCP server packages are pinned and launched from local `node_modules/.bin` when installed; unavailable MCP servers degrade to the Python baseline tools
- destructive Actor shell commands such as recursive delete and hard reset are blocked before local run or bash MCP
- non-zero Actor shell exits are reported as failed tool results
- E2B mode routes Actor shell and deterministic verification through one fail-closed remote backend
- Actor role allowlists are enforced again immediately before local or MCP tool dispatch
- committed root tool-call results are reused during recovery instead of being executed again

Git worktrees and shadow repositories provide version-control and working-directory separation, not an operating-system sandbox. Shell processes still run with the permissions of the current user, so only run the agent against projects and environments you are willing to modify.

SQLite cannot atomically commit an arbitrary shell, filesystem, or network side effect together with its tool-result checkpoint. A crash after the side effect but before the checkpoint can still cause a retry. See [ADR-0002](docs/adr/0002-durable-run-store.md) for the exact transaction boundary.

Graph finalization uses an explicit order: validate required artifact digests,
persist verification while the domain Run remains non-terminal, let LangGraph
commit the final graph checkpoint, and only then mark the RunStore record
completed. Any persistence error is surfaced. The local `AsyncSqliteSaver` is
appropriate for a single-process CLI and is not the production multi-process
checkpointer.

## Development

Run all tests:

```bash
.\.venv\Scripts\python.exe -m pytest
```

Type-check the trusted runtime boundary:

```bash
.\.venv\Scripts\python.exe -m mypy
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

The fixture suite above is a project-specific regression and safety smoke suite,
not a general coding benchmark. Standard coding-agent evaluation is delegated to
Harbor. Install the optional integration and run the continuously refreshed
SWE-rebench dataset with:

```bash
python -m pip install -e ".[benchmark]"
sca-eval harbor --model deepseek/deepseek-v4-pro
```

Run these commands from a Python 3.12 environment; this is also the version used
by the project CI and by the adapter inside Harbor task containers.

`sca-eval harbor` builds the current checkout into a wheel, installs that exact
artifact in each Harbor task container, discovers its task repository, runs the
same SCA core used by CLI/Web headlessly there, and exports token metadata,
JSONL traces, final reports, and Actor artifacts to the Harbor job. The adapter
does not force a benchmark-specific strategy or tool order. See
[evals/README.md](evals/README.md) for dataset selection, forwarded Harbor
options, and the recommended nightly/release cadence.

## Roadmap

Near-term:

- Calibrate task-assessment rules against repeated real-model eval baselines.
- Expand multi-file, recovery, conflict, and fault-injection evals and compare cost per success.

Longer-term:

- Better merge/conflict workflows for Actor diffs.
- More robust model/provider routing.
- Auditable interactive or multi-party approval workflows for destructive/high-risk operations.
