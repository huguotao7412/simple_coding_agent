# Enforced Execution Policy and Run Budgets

## Goal

Turn the advisory `TaskAssessment` into a versioned, deterministic execution
policy that is enforced by the runtime. Planner and Actor model calls share one
durable Run-level budget, while delegation, verification, and repair remain
bounded by the same policy.

## Scope

This increment adds:

1. An immutable `ExecutionPolicy` compiled from `TaskAssessment`.
2. An immutable `ExecutionBudget` and a mutable, lock-protected Run budget
   ledger.
3. Runtime enforcement for wall time, model calls, total tokens, failed tool
   calls, Actor count, Actor steps, repair attempts, required quality gates,
   execution topology, and high-risk approval.
4. Structured policy/budget events and durable checkpoint state.
5. An explicit CLI approval flag for high-risk non-interactive runs.
6. Patch provenance enforcement: only the exact diff of a completed Coder task
   may be merged, and gated strategies require recorded passing evidence.

This increment does not add:

- an approval service or Web approval UI;
- model-based policy classification;
- eval fixture expansion or threshold calibration;
- cross-process budget coordination;
- cancellation of an in-flight provider request when its returned usage crosses
  the token limit.

## Contracts and invariants

- Policy and budget schemas are versioned and JSON serializable.
- The policy is compiled once per new task and cannot be changed by tool calls.
- Resume restores the original policy and cumulative consumption. It never
  recompiles policy from an empty prompt.
- Old checkpoints without policy state continue to load. A resumed legacy Run
  receives no synthetic limits beyond the existing runtime limits.
- All counter check-and-consume operations are atomic under one `asyncio.Lock`.
- Limits are inclusive: consuming exactly the configured amount is allowed;
  the next operation is denied.
- Token usage is charged after the provider returns authoritative/estimated
  usage. Crossing the limit terminates the Run before the response is acted on.
- Wall time uses accumulated active elapsed time plus the current process
  segment, so downtime between interruption and resume is not charged.
- A denial is fail-closed and emits `policy_denied` or `budget_exhausted`.
- Existing role tool allowlists, workspace boundaries, sandbox policy, and
  destructive-command guards remain independent defense layers.
- `planner_direct` cannot invoke delegation or patch application even if the
  model emits a syntactically valid tool call.

## Strategy matrix

| Strategy | Allowed Actor topology | Max Actors | Quality gates |
| --- | --- | ---: | --- |
| `planner_direct` | no delegation | 0 | not required |
| `single_actor` | one Coder | 1 | optional |
| `coder_with_gates` | one Coder | 1 | required and non-empty |
| `scout_then_coder` | one Scout, then one Coder | 2 | optional |
| `scout_then_dag` | Scout/Coder/Verifier DAG | assessment hint, max 4 | optional |

For `scout_then_coder`, the Coder may be submitted in a later delegate call or
in the same call with a dependency on the Scout. A Coder without a completed or
declared Scout dependency is denied.

## Default budgets

Defaults are deliberately conservative, inspectable constants rather than
environment-dependent hidden behavior:

| Resource | direct | single/gated | scout+coder | DAG |
| --- | ---: | ---: | ---: | ---: |
| Planner steps | 20 | 40 | 50 | 60 |
| Actor steps each | n/a | 30 | 40 | 60 |
| Model calls | 20 | 50 | 90 | 180 |
| Total tokens | 80k | 160k | 280k | 600k |
| Active wall time | 5m | 15m | 25m | 45m |
| Failed tool calls | 5 | 10 | 16 | 30 |
| Repair attempts | 0 | 2 | 2 | 3 |

Repository verification configuration may request fewer repair attempts; the
effective limit is the minimum of repository and policy limits.

## Enforcement points

1. `Planner`: compile/install policy, emit it, and restore it on resume.
2. `AgentRuntime`: claim model calls, charge tokens, enforce wall time, and
   charge failed tool calls.
3. `DelegateTool`: validate strategy topology and atomically reserve Actor slots.
4. `WorktreeActorExecutor`: cap Actor steps, require configured quality gates,
   and cap repair attempts.
5. `ApplyPatchTool`: verify task identity, Coder role, terminal status, exact
   diff provenance, and required passing gate evidence.
6. `RunContext`/`SQLiteRunStore`: checkpoint and restore policy plus budget
   ledger state.

## Failure semantics

Budget exhaustion is an expected terminal runtime outcome, not an internal
exception leak. It produces an error event and a failed durable Run. Actor-local
budget exhaustion returns a failed Actor result, allowing DAG dependency
blocking to work normally. High-risk tasks without explicit approval stop before
the first model call.

## Acceptance criteria

- Every new task emits both `task_assessment` and `execution_policy` before its
  first model call.
- Strategy topology cannot be bypassed with a crafted `delegate` call.
- Planner and Actors cannot exceed shared model-call/token/Actor budgets.
- Gated strategy fails if no non-empty quality gate configuration exists.
- A fabricated or unverified diff cannot bypass the gated Actor boundary.
- Repair count is bounded by both repository config and policy.
- Checkpoint round-trips policy and consumption and remains backward compatible.
- Existing tests and strict type checks remain green.
