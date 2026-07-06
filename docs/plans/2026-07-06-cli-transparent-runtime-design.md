# CLI Transparent Runtime Design

## Goal

Make `sca --dir <repo>` a transparent, conservative, locally useful coding agent experience suitable for a resume-grade project. The first milestone prioritizes a reliable CLI runtime over Web UI polish or maximum Actor concurrency.

## Product Positioning

The agent should behave like an auditable pair programmer:

- It explains the current phase and planned action.
- It shows tool calls, key arguments, and results.
- It records failures with useful categories instead of raw exceptions only.
- It reports touched files, verification commands, outcomes, and residual risk.
- It favors traceability and recovery over aggressive autonomous execution.

The implementation should still preserve the existing Planner/Actor direction, but the first milestone is local CLI reliability.

## Architecture

Introduce a shared runtime boundary for the ReAct loop:

- `core/runtime.py` owns the loop: step limits, LLM calls, tool call parsing, tool execution, context compression, repeat-call detection, and event emission.
- `Planner` and `ActorAgent` become thinner role/config wrappers around the shared runtime.
- `AgentEvent` becomes the stable event protocol consumed by CLI and later Web UI.
- CLI rendering consumes events only. It should not know how Planner or Actor internals work.

This changes the project from two duplicated agent loops into one auditable runtime that can drive multiple roles.

## Components

### Agent Runtime

`AgentRuntime` should accept:

- `llm_client`
- `context_manager`
- `workspace_dir`
- `tools_by_name` or `tool_provider`
- `max_steps`
- optional dynamic context builder
- optional actor or role id

It should expose `run()` and `run_stream()` compatible with existing callers.

### Tool Call Parser

Centralize tool argument parsing into a small helper. It should:

- Strip accidental markdown code fences.
- Treat empty args as `{}`.
- Reject non-object JSON by converting to `{}` with a recoverable error.
- Return structured parse results so invalid JSON does not crash the loop.

### Tool Executor

Centralize tool execution behavior:

- Inject `workspace_dir` consistently for local tools.
- Route through MCP provider when present.
- Handle unknown tools.
- Convert exceptions into `ToolResult.fail`.
- Record observations into `ContextManager`.

### Event Protocol

Keep event names simple and stable:

- `thought`
- `tool_call`
- `tool_result`
- `compaction`
- `state_update`
- `verification`
- `error`
- `done`
- `token_stats`

Events should include enough data for CLI and Web to render without understanding the agent loop.

## Data Flow

```text
User Input
  -> Planner/Actor configuration
  -> AgentRuntime.run_stream()
  -> LLM response
  -> ToolCallParser
  -> ToolExecutor
  -> ContextManager observation
  -> AgentEvent stream
  -> CLI renderer
  -> Final report
```

## Error Handling

The first milestone should classify failures into practical categories:

- LLM API errors
- invalid tool-call JSON
- unknown tools
- tool execution errors
- MCP startup or connection errors
- file security/path errors
- repeated tool-call circuit breaker
- context compaction failure
- max-step exhaustion

Each error should produce a structured event and a user-readable message.

## Testing Strategy

Add tests that do not depend on real LLM calls:

- Mock LLM returns a final answer.
- Mock LLM returns one valid tool call and then a final answer.
- Mock LLM returns invalid JSON arguments.
- Repeated tool calls trigger the circuit breaker.
- Max-step exhaustion stops the loop.
- Streaming event order remains stable.

Keep existing MCP integration tests as smoke tests, but the core runtime should be covered with fast deterministic unit tests.

## Non-Goals For First Milestone

- Rebuilding the Web UI.
- Making multi-Actor concurrency the primary product experience.
- Fully automatic conflict resolution.
- Adding broad new tool permissions.
- Building a benchmark suite before the runtime is stable.

## Success Criteria

- `Planner` and `ActorAgent` no longer duplicate the core ReAct loop.
- CLI can render a clear sequence of thoughts, tool calls, results, errors, and final output.
- Runtime behavior is covered by deterministic tests.
- Existing tests keep passing.
- The design remains compatible with later multi-Actor and Web UI improvements.
