# State Ownership

| State | Authority | Persistence |
|---|---|---|
| lifecycle, conversation, immutable policy/budget | `RunAggregate` | aggregate checkpoint |
| task graph, approvals, completed calls, artifacts, verification, usage/security refs | `RunAggregate` | aggregate checkpoint |
| workflow version/stage, ready/active/completed IDs, interrupt and domain refs | GraphState | LangGraph checkpointer |
| serialized compatibility envelope | `RunCheckpoint` | RunStore adapter |

`RunCheckpoint` is always derived from `RunAggregate`. Schema 1 checkpoints are
migrated to schema 2 by wrapping their policy, budget, task graph, messages,
completed-call cache and usage in an aggregate snapshot. Unknown versions or
malformed policy/budget fail closed. Resume restores the original policy,
budget ledger, task graph and sanitized completed-call cache. A completed call
does not replay; a pending call is reauthorized; single-use approval consumption
is not reset.

GraphState must never contain a full conversation, diff, tool output,
verification log, provider/session instance, or secret. Compatibility fields
are bounded summaries or references and are guarded by size and dependency
tests.

