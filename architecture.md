# Simple Coding Agent Architecture

This document describes the runtime boundaries that make Simple Coding Agent auditable: the Planner/Actor lifecycle, worktree isolation, MCP tool boundary, and local eval design.

## Runtime Lifecycle

```text
User prompt
  -> Planner
  -> AgentRuntime
  -> LLM streaming response
  -> tool-call parser
  -> tool executor
  -> AgentEvent stream
  -> CLI renderer / eval trace writer
```

`AgentRuntime` is the shared ReAct loop. Planner and Actor agents both use it, so step limits, tool-call parsing, malformed JSON recovery, repeated-action circuit breaking, context compression, token reporting, and event emission live in one place.

The Planner owns orchestration. It decomposes work, records tasks in `GlobalState`, delegates isolated subtasks, receives Actor summaries and diffs, applies selected patches, and synthesizes the final response.

Actors own execution. Each Actor receives one concrete task plus scoped context, runs in its own git worktree, and reports a summary plus an extracted diff.

## Planner / Actor Flow

1. The Planner receives the user request.
2. For unfamiliar projects, it can delegate a read-only Scout task.
3. For code changes, it creates coder tasks and verifier tasks.
4. `delegate` creates one worktree per Actor.
5. Dependency diffs are applied to dependent Actor worktrees as a committed baseline.
6. The Actor runs with role-specific prompts and tool allowlists.
7. The Actor worktree diff is extracted with `git diff --cached --binary`.
8. The Planner reviews and applies successful diffs to the main workspace.

The dependency baseline matters because verifier tasks must see the coder changes they are validating, while their own final diff should contain only verifier-created artifacts such as tests.

## Worktree Isolation

Each delegated Actor gets a throwaway branch under `.worktrees/`.

This provides:

- filesystem separation from the main workspace
- independent diffs per subtask
- safer concurrent execution
- cleanup after Actor completion
- recovery from abandoned worktrees on startup

The main workspace remains the merge point. `apply_patch` applies selected Actor diffs to the working tree but does not commit automatically, so users can inspect changes before deciding how to version them.

## MCP Tool Boundary

Actor tools are served through MCP providers bound to the Actor worktree:

- filesystem MCP server for file operations
- bash MCP server for shell execution
- local helper tools for code search, outlines, and directory listing

The provider sets the MCP subprocess current working directory to the Actor worktree and performs defense-in-depth path validation for absolute filesystem paths. Actor roles receive different allowlists:

- Scout: read-only exploration
- Coder: implementation tools
- Verifier: read, test, and test-file creation tools

## Event And Trace Model

The runtime emits `AgentEvent` records for:

- streamed thought/content tokens
- tool calls and tool results
- Actor task updates
- context compaction
- token usage
- errors
- final completion

The CLI uses this stream for transparent terminal rendering. The eval runner persists the same stream as JSONL at:

```text
tmp/eval-runs/<task_id>/.sca/traces/run_trace.jsonl
```

This makes each run inspectable after the fact without changing the runtime loop.

## Eval Design

The local eval suite is intentionally deterministic and offline at check time.

`sca-eval run --model <model>` performs the full measurable loop:

1. copy fresh fixture repositories into `tmp/eval-runs/`
2. run the agent against each task prompt
3. write `.sca/final_report.md` in each candidate workspace
4. persist `.sca/traces/run_trace.jsonl`
5. evaluate allowed file changes, required content, report terms, and pytest results
6. write aggregate `eval_results.json`

`eval_results.json` records pass/fail, duration, tool-call counts, token counts, trace path, report path, final output, and failure reasons per task.

This keeps the project measurable: changes to prompts, runtime logic, model selection, or tool policy can be compared by pass rate, cost proxy, runtime, and failure mode.
