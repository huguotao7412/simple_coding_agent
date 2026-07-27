# ADR-0007: Structured LangGraph lifecycle as the sole control plane

Status: accepted

## Decision

LangGraph is SCA's only top-level control plane. CLI, interactive CLI, Web Live
Agent, local eval, and Harbor depend on the framework-neutral
`ApplicationService` protocol and receive `LangGraphOrchestrator` from one
factory. The legacy adapter, `SCA_ORCHESTRATOR`, and `--orchestrator` selector
are removed. A pre-LangGraph Run remains inspectable, but resume fails with an
explicit migration reason because no safe graph program counter exists.

The graph lifecycle is:

```text
START -> assess_task -> compile_policy -> approval_router
 -> request_human_approval -> plan -> validate_plan
 -> schedule_ready_actors -> execute_actor -> collect_actor_results
 -> verify -> repair_router -> bounded_repair
 -> finalize_success | finalize_failure -> END
```

The approval branch is skipped for low-risk work. Planner-direct work uses a
direct execution branch after the same assessment, policy, planning, and
validation stages.

## Planning, execution, and verification boundaries

`plan` compiles a compact versioned DAG. `validate_plan` treats that DAG as
untrusted and checks Actor count, role allowlist, dependency references,
acyclicity, verification requirements, repair limit, and target paths against
the immutable `ExecutionPolicy` and workspace root. A plan cannot add authority.

`schedule_ready_actors` computes one ready batch. LangGraph `Send` fans that
batch out dynamically. Each `execute_actor` call invokes one full existing
`ActorExecutor`/`AgentRuntime` ReAct loop. Actor tool authorization, path and
command checks, worktrees, sandbox selection, budgets, tool-call result cache,
artifact storage, and verification remain in the data plane. Token chunks and
individual tool calls are not graph nodes.

`ExecutionPolicy.continue_independent_branches` explicitly decides whether a
failed Actor stops unrelated branches. Failed dependencies always block their
descendants; successful sibling results and artifacts remain committed.

The existing delegate/Actor adapter remains the execution policy enforcement
point and the existing worktree verifier retains bounded repair where the real
workspace and evidence are available. The graph owns lifecycle routing and
repair accounting, not a duplicate verifier implementation.

## State and replay

Graph State schema 2 is JSON/msgpack-safe and limited to 256 KiB. It contains
run/thread identity, request, assessment, immutable policy snapshot, approval,
validated DAG, ready/active/completed/failed/blocked Actor IDs, attempt metadata,
compact Actor outcomes, A2A references, artifact references and digests,
verification/usage summaries, repair counters, failure category, and terminal
references. It excludes full diffs, full logs, large conversations, clients,
providers, paths as objects, locks, queues, connections, and executable objects.

`migrate_graph_state` is the explicit schema migration entry. It may normalize
known data shapes but never invent a program counter or permissions. Policy is
reconstructed from the trusted RunStore snapshot and graph plans are revalidated.
Artifact paths are resolved under the workspace/state roots and SHA-256 checked.

Actor replay first checks durable task status. Completed Actors are not run
again. Root tool calls still use `tool_call_id` results; Actor/task IDs are
stable inside the plan. LangGraph pending writes preserve successful sibling
writes when another task in the same superstep fails, as documented by
LangGraph. None of these mechanisms makes shell, filesystem, or network effects
exactly-once.

## Persistence and visibility

The LangGraph checkpointer owns program counter, interrupts, pending writes, and
compact workflow state. RunStore owns domain status, immutable policy, budget,
task/Actor ledger, completed root tool results, events, reports, and artifact
index. `run_id` equals `thread_id`.

Success becomes visible in this order:

1. required artifacts exist, remain in bounds, and match their digest;
2. verification and a non-terminal domain checkpoint are persisted;
3. LangGraph commits the final graph checkpoint and reaches `END`;
4. RunStore transitions to `completed`;
5. buffered terminal `done` is released to the caller.

Any checkpointer, RunStore, or artifact error is explicit. This ordering is a
small commit protocol, not a distributed transaction with external effects.
`AsyncSqliteSaver` is suitable for local single-process CLI use and tests; it is
not the multi-process production checkpointer.

## Approval and concurrency

High-risk work interrupts before the first model call and before Actor/tool side
effects. The payload contains run/thread identity, assessment and policy schema,
risk and reasons, requested capabilities, target scope, policy limits, and the
fact that model planning occurs only after approval. Resume uses
`Command(resume=...)` on the same thread. Approval only satisfies one policy
predicate and cannot expand roles, tools, paths, commands, budgets, or sandbox
capabilities.

Interrupt code before `interrupt()` is idempotent because LangGraph re-enters
the node from its start. UI event-loop transports are recreated per invocation;
durable state stays in the checkpointer and RunStore. A production service must
add a cross-process resume claim/idempotency key around concurrent approval
clicks.

## Known limits

- Deterministic planning currently selects a policy-shaped topology; learned
  multi-branch decomposition remains future work.
- SQLite does not provide multi-process production coordination.
- A crash after an external effect and before its durable result can retry it.
- Cross-process outbox/lease and domain-specific compensation are not present.
