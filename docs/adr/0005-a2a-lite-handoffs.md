# ADR-0005: Versioned A2A_lite Handoffs and Artifact References

## Status

Accepted on 2026-07-15.

## Context

The Planner/Actor runtime already used immutable task/result contracts, a DAG
scheduler, durable task snapshots, and patch artifacts. Semantic context between
dependent Actors was still copied into free-form `context_summaries`, while code
changes travelled separately through fields on shared task state. This made the
handoff implicit, difficult to version, and easy for the Planner to omit.

The runtime is currently an in-process modular monolith. A network transport,
broker, service discovery, authentication, and delivery acknowledgements would
add operational complexity without improving the current execution topology.

## Decision

Introduce `core/a2a_lite/` as a transport-independent domain contract:

- `AgentMessage` is an immutable, versioned envelope with message, run, task,
  sender, recipient, and correlation identifiers.
- `AgentHandoff` separates findings, decisions, constraints, unresolved
  questions, and artifact references.
- `ArtifactRef` identifies patches, verification evidence, reports, or files by
  URI, media type, producer, and optional SHA-256 digest.
- `DelegateTool` persists the completed/failed handoff on the task node, emits
  the same envelope into the run event stream, and automatically injects parent
  handoffs into a ready dependent task.
- `WorktreeActorExecutor` keeps large payloads outside prompts and renders only
  the structured handoff plus artifact metadata. Dependency patches continue to
  be applied as the dependent worktree baseline.

The initial schema is `a2a-lite/1.0`. Unknown schema versions fail explicitly on
deserialization rather than being silently interpreted.

## Consequences

### Positive

- Dependent Actors receive upstream semantics automatically.
- Prompt context distinguishes findings, decisions, constraints, open questions,
  and external artifacts.
- Handoffs survive run checkpoints and can be replayed from the event stream.
- Patch integrity can be checked without embedding the full diff in the handoff.
- A future process or network adapter can serialize the same contract.

### Negative

- Task snapshots now contain an additional versioned message object.
- Schema changes require an explicit compatibility or migration decision.
- Legacy `context_summaries` remain temporarily supported, so both input styles
  must be tested during migration.

## Deferred

Message queues, acknowledgements, retries, cancellation, capability discovery,
authentication, and compatibility with an external A2A transport standard are
deferred until the runtime has a real multi-process or remote-Agent use case.
