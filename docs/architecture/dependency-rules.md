# Dependency Rules

```mermaid
flowchart LR
    D["domain"] --> A["application"]
    D --> P["ports"]
    A --> I["CLI / Web / LangGraph adapter"]
    P --> X["adapters: SQLite, MCP, sandbox, Git, Guardrails"]
```

The arrows mean “is used by”. Domain code cannot import adapters, CLI/Web,
LangGraph, MCP, SQLite, Guardrails or sandbox implementations. Ports cannot
import adapters. Application code cannot import `sqlite3`, MCP sessions,
Guardrails third-party types, E2B or subprocess details.

`tests/test_architecture_dependencies.py` enforces framework/import placement
and thin compatibility facades. Runtime tests prove that provider tools and
verification commands enter `ToolGateway` rather than calling transports
directly.

