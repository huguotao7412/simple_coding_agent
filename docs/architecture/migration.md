# Compatibility and Migration

Compatibility facades:

| Old import | Delegates to |
|---|---|
| `core.orchestration.langgraph` | orchestration adapter/graph builder |
| `core.runtime.engine.AgentRuntime` | `runtime.model_loop.ModelLoop` |
| `core.runs.sqlite_store` | SQLite RunStore adapter |
| `core.security.openai_guard` | OpenAI Guardrails adapter |
| `SecurityMiddleware` / `ToolPolicy` | deterministic authorization compatibility names |

CLI, Web, eval runner, Planner, ActorAgent and AgentRuntime public entry points
remain supported. No runtime dependency installation occurs. The optional
Guardrails dependency is required only for configured external inspection.

Checkpoint migration is versioned and non-destructive. Schema 1 is converted to
schema 2 without inventing policy or budget. Unsupported/corrupt checkpoints
raise a corruption error; they never create an unlimited policy. SQLite table
shape remains compatible because the versioned envelope stays in
`checkpoint_json`.

