# ADR-0006: LangGraph as the durable control plane

Status: accepted (LangGraph is the default)

## Context and decision

SCA already has a secure execution data plane: `AgentRuntime`, execution-time
tool authorization, workspace/path guards, Actor worktrees, sandbox adapters,
budgets, artifact storage, deterministic verification, and `RunStore`. Replacing
that code would discard tested safety boundaries. We therefore add LangGraph only
around the top-level lifecycle.

`core.orchestration.Orchestrator` is the framework-neutral application port.
`LegacyOrchestrator` wraps the previous Planner lifecycle.
`LangGraphOrchestrator` owns assessment, policy compilation, approval routing,
Planner/Actor execution as one coarse node, verification routing, finalization,
checkpointing, and resume. It is the default for interactive/non-interactive CLI,
the Web Live Agent, local eval, and Harbor runs.

The Planner/Actor execution node deliberately invokes the complete existing
Planner and Actor ReAct loops. Individual model tokens and tool calls are not graph
nodes. Actor quality-gate repair remains inside `WorktreeActorExecutor`, where the
actual worktree and verification evidence exist.

Interactive entry points create one durable graph thread per user request through
`InteractiveOrchestrationSession`. They carry only bounded user/assistant history
between tasks, keeping policy, budget, task DAG, approval, and tool-result recovery
scoped to the individual Run.

## Control plane and data plane

LangGraph may decide *when* an Actor runs, but it does not grant tool authority.
`ExecutionPolicy`, role allowlists, MCP/local dispatch checks, path validation,
destructive-command guards, `SandboxBackend`, verification, and budget charging
remain enforced at the execution entry point. Graph state, model output, tool
output, dynamic names, commands, paths, and artifact references are untrusted.

The local sandbox is explicitly not OS isolation. Selecting E2B still fails closed
when E2B is unavailable; LangGraph does not add a host fallback.

## Graph state

Schema `1` contains bounded JSON-compatible values: stable `run_id/thread_id`,
request, assessment and policy snapshots, structured approval, compact plan and
Actor ID summaries, A2A/artifact references, verification and usage summaries,
repair counters, failure category, and terminal output/status. It rejects a
run/thread mismatch and state larger than 256 KiB.

Full diffs, conversations, tool output, and verification logs remain in existing
RunStore/artifact storage. Artifact URIs are resolved again and must remain inside
the workspace or workspace-keyed state root; missing references fail recovery.
Checkpoint deserialization uses LangGraph's non-pickle serializer with strict
msgpack module loading enabled.

## Persistence ownership

LangGraph's checkpointer is the source of truth for graph program counter,
interrupts, pending graph writes, and compact graph state. Local runs use
`AsyncSqliteSaver` at the workspace-keyed user state root; tests can use
`InMemorySaver`. `run_id` maps unchanged to LangGraph `thread_id`.

`RunStore` remains the source of truth for domain run status, immutable execution
policy and budget consumption, conversation checkpoint, task ledger, completed
root tool-call cache, reports, artifacts, and audit events. Both stores correlate
on the same run ID. Old runs remain inspectable. A legacy run must be resumed by
the legacy adapter because it has no LangGraph checkpoint; no synthetic graph
position is invented.

The graph suppresses terminal `done` delivery until its final checkpoint succeeds.
A checkpointer failure is returned explicitly and must not be presented as
success. Artifact existence is validated before final success. This is not a
distributed transaction between SQLite, the filesystem, a shell, and a network.

## Approval semantics

High-risk assessment reaches `interrupt()` before the first model call and before
Actor/tool execution. The payload contains run ID, risk level/reasons, requested
capabilities, target scope, and a policy summary. Resume uses `Command(resume=...)`
with the same thread ID. Approval is persisted in RunStore and the graph checkpoint
and added to the audit trail. Rejection finalizes without invoking the model.

Approval only satisfies the policy's approval predicate. It cannot expand Actor
roles, budgets, paths, commands, tool allowlists, or sandbox capabilities.

## Replay and idempotency

LangGraph nodes can replay. Code before `interrupt()` is therefore idempotent:
the paused RunStore transition and audit event use current status/deterministic
event keys to avoid duplicate records. Completed graph supersteps are recovered
from the checkpointer. Root tool calls continue to reuse the existing
`tool_call_id` result cache, and successful parallel node writes can be recovered
by LangGraph pending writes.

Neither LangGraph nor SQLite provides exactly-once external effects. A process can
still crash after an external effect and before its result is durably recorded.
Such operations need domain idempotency keys, a durable result cache, or an
explicit compensation protocol.

## Migration and rollback

The migration is additive:

1. keep the framework-neutral port;
2. use LangGraph by default across CLI, Web Live Agent, eval, and Harbor;
3. retain the minimal legacy wrapper only for pre-LangGraph Run recovery and
   explicit emergency rollback;
4. move shared lifecycle logic behind the port instead of maintaining divergent
   business rules;
5. remove the compatibility wrapper after the old-Run support window closes.

Rollback selects `legacy`; it does not delete LangGraph checkpoints or RunStore
history. The rejected alternative was an overall rewrite of Planner, Actor,
runtime, tools, and verification as fine-grained graph nodes. It increased state
size and replay surface while weakening the tested execution boundary.
