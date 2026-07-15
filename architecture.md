# Simple Coding Agent Architecture

This document describes the runtime boundaries that make Simple Coding Agent auditable: the Planner/Actor lifecycle, worktree isolation, MCP tool boundary, and local eval design.

## Core Package Boundaries

The modular monolith groups cohesive implementation details without hiding dependencies behind package-wide re-exports:

- `core/runtime/`: the ReAct execution engine and conversation compaction.
- `core/runs/`: durable-run models, task state, `RunContext`, the store port, and SQLite adapter.
- `core/actors/`: Actor behavior, execution contracts, roles, and the worktree adapter.
- `core/a2a_lite/`: versioned Agent messages, structured handoffs, and artifact references.
- `core/verification/`: deterministic gate configuration, subprocess execution, evidence, and repair prompts.
- `core/execution/`: versioned task assessment contracts and deterministic strategy selection.
- `core/sandbox/`: command-execution port, local/E2B adapters, and guarded workspace transport.
- `core/events.py`: the cross-domain event contract shared by runtime, runs, MCP, CLI, and evals.
- `core/planner.py`: the application orchestration entry point.

```mermaid
flowchart TD
    PLANNER["planner"] --> RUNTIME["runtime"]
    PLANNER --> RUNS["runs"]
    PLANNER --> TOOLS["tools"]
    RUNTIME --> RUNS
    RUNTIME --> TOOLS
    TOOLS --> ACTORS["actors"]
    TOOLS --> RUNS
    ACTORS --> RUNTIME
    ACTORS --> RUNS
    PLANNER --> EVENTS["events contract"]
    RUNTIME --> EVENTS
    RUNS --> EVENTS
    MCP["mcp"] --> EVENTS
```

Package `__init__.py` files stay minimal. Callers import the owning module explicitly, for example `core.runtime.engine`, `core.runs.context`, or `core.actors.contracts`; this keeps dependency review searchable and prevents accidental circular imports through convenience re-exports.

## Runtime Lifecycle

```text
User prompt
  -> TaskAssessor
  -> Planner
  -> AgentRuntime
  -> LLM streaming response
  -> tool-call parser
  -> tool executor
  -> AgentEvent stream
  -> CLI renderer / eval trace writer
```

`AgentRuntime` is the shared ReAct loop. Planner and Actor agents both use it, so step limits, tool-call parsing, malformed JSON recovery, repeated-action circuit breaking, context compression, token reporting, and event emission live in one place.

Before a new Planner turn enters that loop, `TaskAssessor` performs a bounded,
read-only workspace scan and classifies intent, complexity, and risk. It publishes a
versioned `task_assessment` event and injects the same JSON as durable system context.
The assessment is deterministically compiled into an enforced `ExecutionPolicy`
rather than remaining only a prompt recommendation.

The Planner owns orchestration. Each Planner receives a `RunContext` with an independent task ledger, run ID, event queue, and usage accumulator. It decomposes work, delegates isolated subtasks, receives Actor summaries and diffs, applies selected patches, and synthesizes the final response.

Actors own execution. Each Actor receives one concrete task plus scoped context, runs in its own git worktree, and reports a structured A2A_lite handoff plus an extracted diff. The full extracted diff is persisted as a patch artifact, while the handoff carries typed artifact references instead of copying large payloads into prompts.

## Execution Policy and Budget Boundary

`ExecutionPolicy` defines Actor topology and roles, required quality gates, high-risk approval, and budgets for Planner/Actor steps, model calls, total tokens, failed tool calls, repairs, and active wall time. Tool calls cannot override this immutable task policy.

One lock-protected `RunBudgetLedger` is shared by Planner and all nested Actors. Model calls reserve capacity before dispatch and provider usage is charged after the response; a response that crosses the token limit is not allowed to drive tool execution. Delegation atomically reserves Actor capacity and distinguishes started roles from successfully completed roles, so a failed Scout cannot unlock a later Coder. `scout_then_coder` and `scout_then_dag` dependency shapes are enforced in `DelegateTool`, not left to prompt compliance.

Policy and consumption snapshots are stored in `RunCheckpoint`. Resume continues with the original policy and cumulative consumption, while process downtime is excluded from active wall time. Legacy checkpoints without these fields remain loadable. Interactive REPL turns receive fresh task policy scopes; durable non-interactive Runs cannot be reclassified during resume, although an external CLI approval may satisfy a previously missing high-risk authorization.

Policy and budget failures are fail-closed and emit `policy_denied` or `budget_exhausted`. This orchestration layer complements rather than replaces role tool allowlists, worktrees, sandboxing, path boundaries, and destructive-command guards. See the [execution policy plan](docs/plans/2026-07-15-enforced-execution-policy.md) for defaults and trade-offs.

Merging into the main workspace is policy protected as well. `ApplyPatchTool` requires a completed Coder task and an exact match with the full diff held in task state; `coder_with_gates` additionally requires recorded passing verification evidence. A model cannot bypass the Actor/verification boundary by inventing a task ID or constructing its own diff.

## Delegation Execution Boundary

P1 does not change the external system context or deployment topology. `ActorExecutor` is an in-process port inside the Python Agent container, not a new service. The boundary separates application scheduling from single-Actor infrastructure execution:

```mermaid
flowchart LR
    PLANNER["Planner"] --> DELEGATE["DelegateTool\nDAG scheduler"]
    DELEGATE --> PORT["ActorExecutor\nport"]
    PORT --> WORKTREE["WorktreeActorExecutor\ndefault adapter"]
    DELEGATE --> STATE["RunContext / TaskState"]
    WORKTREE --> GIT["Git worktree + diff"]
    WORKTREE --> MCP["Per-Actor MCP provider"]
    WORKTREE --> ACTOR["ActorAgent / AgentRuntime"]
    WORKTREE --> ARTIFACT["Patch artifact store"]
```

`DelegateTool` owns validation, DAG readiness, concurrency, dependency blocking, task-state transitions, exception isolation, and result rendering. It communicates through immutable `ActorTaskSpec` and `ActorExecutionResult` values.

`WorktreeActorExecutor` owns context injection, one-time orphan cleanup, worktree setup, dependency baselines, MCP startup and shutdown, Actor construction, diff extraction, artifact persistence, and worktree cleanup. Context file paths are resolved against both the main workspace and Actor worktree before any pre-MCP read or copy, preventing absolute-path and `..` traversal from bypassing the tool policy.

## A2A_lite Communication Contract

`A2A_lite` is an in-process, transport-independent communication contract. Every completed or failed Actor execution produces an immutable `AgentMessage` using schema `a2a-lite/1.0`. Its `AgentHandoff` separates findings, decisions, constraints, unresolved questions, and `ArtifactRef` values. Patch references include a content digest; verification log references retain the producing task and gate metadata.

The message is stored in the task snapshot, emitted as an `a2a_lite_message` event, returned to the Planner, and automatically attached to ready dependent tasks. The downstream prompt receives the serialized structured envelope and artifact metadata; the full patch remains in the per-user runtime state directory and is independently applied as the worktree baseline. Legacy `context_summaries` remain accepted for compatibility but are no longer required for dependency handoff.

This phase intentionally adds no broker, network transport, discovery, authentication, acknowledgement, or retry protocol. See [ADR-0005](docs/adr/0005-a2a-lite-handoffs.md).

For coder tasks, it is also the deterministic verification boundary. If `.sca/quality-gates.toml` exists, configured commands run sequentially inside the Actor worktree with `shell=False`. Required failures are converted into bounded repair turns on the same Actor context, then rerun by the runtime rather than trusted on the Actor's claim. Repeated failure fingerprints terminate early as no progress. Only a passing coder diff is exported; every attempt retains a complete log in the per-user runtime state directory and a compact structured report in `ActorExecutionResult`.

The default adapter currently reads dependency diffs through `RunContext.state`. This keeps P1 backward compatible, but a future durable or remote executor should receive dependency artifacts through a narrower execution context or directly in the task specification. See [ADR-0001](docs/adr/0001-actor-executor-boundary.md).

## Planner / Actor Flow

1. The Planner receives the user request.
2. For unfamiliar projects, it can delegate a read-only Scout task.
3. For code changes, it creates coder tasks and verifier tasks.
4. `delegate` creates one worktree per Actor.
5. Dependency diffs are applied to dependent Actor worktrees as a committed baseline.
6. The Actor runs with role-specific prompts and an execution-time enforced tool policy.
7. The Actor worktree diff is extracted with `git diff --cached --binary`.
8. The diff is stored in the workspace-keyed user state directory, and an A2A_lite handoff records its typed artifact reference.
9. The versioned handoff is persisted, emitted, and automatically injected into dependent Actors.
10. The Planner reviews and applies successful diffs to the main workspace.

The dependency baseline matters because verifier tasks must see the coder changes they are validating, while their own final diff should contain only verifier-created artifacts such as tests.

## Worktree Isolation

Each delegated Actor gets a throwaway branch and a Git worktree. A clean Git
workspace uses the project's repository directly and stores Actor worktrees under
`.worktrees/`. A non-Git workspace, or a Git workspace with uncommitted changes,
uses a process-owned shadow repository under the operating system temporary
directory. The shadow repository snapshots the visible workspace once and creates
ordinary Git worktrees from that baseline; it never initializes or writes Git
metadata into the user's workspace.

The shadow snapshot excludes runtime metadata and common generated dependency or
cache directories. Existing `.gitignore` rules still control which copied files are
committed into the Actor baseline. The trusted host records hashes for the original
workspace files. Before applying an Actor patch, only paths touched by that patch
are compared with the baseline, and concurrent user changes fail closed instead of
being overwritten. Successful patches advance those hashes for subsequent merges.
Process-owned shadow repositories are removed during normal interpreter shutdown;
each Actor worktree is still removed immediately after execution.

This provides:

- filesystem separation from the main workspace
- independent diffs per subtask
- safer concurrent execution
- cleanup after Actor completion
- recovery from abandoned worktrees on startup

The main workspace remains the merge point. `apply_patch` applies selected Actor diffs to the working tree but does not commit automatically, so users can inspect changes before deciding how to version them.

Full Actor patches are retained outside the target workspace under the per-user SCA state root. This gives the Planner a compact context preview without losing the complete artifact needed for audit, retry, and conflict-resolution workflows.

## MCP Tool Boundary

Actor tools are served through MCP providers bound to the Actor worktree:

- filesystem MCP server for file operations
- bash MCP server for shell execution
- local helper tools for code search, outlines, and directory listing

The provider sets the MCP subprocess current working directory to the Actor worktree and performs defense-in-depth path validation for absolute filesystem paths. Actor roles receive different allowlists. The allowlist filters schemas for model guidance and is checked again inside `call_tool()` before local or MCP dispatch, so a directly constructed hidden tool call is denied.

MCP server packages are pinned in `package.json`. At runtime the provider prefers local `node_modules/.bin` executables and falls back to `npx --no-install <package>@<version>`, avoiding unpinned runtime downloads.

Before routing commands to bash MCP, the provider rejects destructive shell patterns such as recursive delete, `git reset --hard`, and `git clean -fd`. These failures are returned as ordinary tool results so the Actor can report the blocked operation instead of mutating the workspace.

## Sandbox Execution Boundary

Worktree isolation and OS execution isolation are separate, composable boundaries.
The trusted host owns worktree setup, dependency baselines, Git staging, diff
extraction, and cleanup. `SandboxBackend` owns untrusted Actor shell and repository
verification commands.

```mermaid
flowchart LR
    HOST["Trusted host"] --> WT["Git worktree"]
    WT --> SB["SandboxBackend"]
    SB --> LOCAL["Local adapter\nnot isolated"]
    SB --> E2B["E2B adapter\nremote Linux sandbox"]
    E2B --> SYNC["bounded workspace archive\nsecrets excluded"]
    WT --> DIFF["Host Git diff extraction"]
```

In E2B mode the provider does not start bash MCP. A foreground `run` adapter sends
the shell string through the remote backend, while verification sends argv through
the same backend. Host filesystem edits are uploaded before each command and remote
changes are safely applied afterward. Missing SDK/key or transfer validation failure
is terminal and never falls back to host execution. See
[ADR-0004](docs/adr/0004-sandbox-execution-boundary.md).

A worktree is not an OS sandbox. It isolates branches, diffs, and default working directories, but Actor subprocesses still have the current user's operating-system permissions. The command and path policies are defense-in-depth controls, not a claim of complete process containment.

- Scout: read-only exploration
- Coder: implementation tools
- Verifier: read, test, and test-file creation tools

## Event And Trace Model

The runtime emits `AgentEvent` records for:

- streamed thought/content tokens
- tool calls and tool results
- policy denials
- Actor task updates
- context compaction
- per-call model usage and whole-run token totals
- errors
- final completion

Every event carries a `run_id` plus task/Actor correlation metadata. Planner and nested Actors publish to the same run-scoped queue, so the CLI and eval trace see the full execution tree. Usage prefers provider-reported counts; when a provider omits usage, the locally counted fallback is persisted with `usage_estimated=true`.

The CLI uses this stream for transparent terminal rendering. The eval runner persists the same stream as JSONL at:

```text
tmp/eval-runs/<task_id>/.sca/traces/run_trace.jsonl
```

This makes each run inspectable after the fact without changing the runtime loop.

The Streamlit dashboard reads the same eval artifacts without invoking the agent: `eval_results.json` for aggregate metrics, trace JSONL for the timeline, `.sca/final_report.md` for the run report, and `.sca/artifacts/actor-diffs/*.patch` for Actor diffs. This keeps observability independent from live model access.

## Durable Run Recovery

Non-interactive `--prompt` runs use a `RunStore` port backed by a workspace-keyed `runs.db` in the per-user SCA state directory. Reports, Actor patches, and verification logs use the same external root. The target workspace only owns optional configuration such as `.sca/quality-gates.toml`. The SQLite adapter owns schema setup, WAL configuration, checkpoint JSON encoding, event ordering, and optimistic version checks. `RunContext` owns the current record, full task snapshot, usage totals, and committed root tool-call results.

The root runtime checkpoints only complete message boundaries. Nested Actors share usage and task state but do not overwrite the root conversation checkpoint. On cancellation the run becomes `paused`; `sca resume <run_id>` reconstructs the conversation, task state, usage, and completed-call cache before continuing.

```mermaid
flowchart LR
    CLI["sca --prompt / resume"] --> P["Planner"]
    P --> R["AgentRuntime"]
    R --> RC["RunContext"]
    RC --> RS["RunStore port"]
    RS --> DB["SQLite runs.db"]
    R --> T["Tools / ActorExecutor"]
```

Committed tool-result checkpoints prevent replay of the same root tool-call ID. This is not global exactly-once execution: an external side effect can succeed immediately before a process crash and before SQLite records its result. See [ADR-0002](docs/adr/0002-durable-run-store.md).

## Eval Design

The local eval suite is intentionally deterministic and offline at check time.

`sca-eval run --model <model>` performs the full measurable loop:

1. copy fresh fixture repositories into `tmp/eval-runs/`
2. run the agent against each task prompt
3. write `.sca/final_report.md` in each candidate workspace
4. persist `.sca/traces/run_trace.jsonl`
5. evaluate allowed file changes, required content, forbidden paths, report terms, and pytest results
6. write aggregate `eval_results.json`

`eval_results.json` records pass/fail, duration, tool-call counts, token counts, trace path, report path, final output, and failure reasons per task.

Safety-oriented tasks can seed uncommitted workspace changes before the agent run, assert that forbidden paths such as parent-directory escape targets were not created, and verify that destructive command requests do not remove protected files.

`sca-eval compare` accepts two or more aggregate result files and writes a Markdown comparison report. The first file is treated as the baseline; later runs are compared by pass rate, duration, tool calls, failed tools, token usage, and task-level regressions/improvements.

This keeps the project measurable: changes to prompts, runtime logic, model selection, or tool policy can be compared by pass rate, cost proxy, runtime, and failure mode.
