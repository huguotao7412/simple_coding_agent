# Remote Sandbox Execution P1

## Objective

Run Actor shell commands and deterministic verification in E2B without requiring a
local container runtime, while preserving host-owned Git worktrees.

## Deliverables

1. Typed `SandboxBackend` lifecycle and execution contracts.
2. Fail-closed `E2BSandboxBackend` with one persistent session per Actor worktree.
3. Bounded, credential-aware workspace archive transport with path validation.
4. Shared Actor shell and verification execution evidence.
5. `sca sandbox-check` diagnostics for SDK, key, template, and network policy.
6. Deterministic tests using fake E2B sessions; live cloud tests remain opt-in.

## Configuration

```text
SCA_SANDBOX_BACKEND=local|e2b
E2B_API_KEY=e2b-...
SCA_E2B_TEMPLATE=base
SCA_E2B_ALLOW_INTERNET=false
SCA_SANDBOX_MAX_TIMEOUT=300
SCA_SANDBOX_MAX_TRANSFER=50000000
```

## Acceptance criteria

- E2B mode never starts host bash MCP and never falls back to a host shell.
- Secrets and Git metadata are excluded from both transfer directions.
- Remote paths and archive sizes are validated before host writes.
- Remote sessions are killed during Actor cleanup.
- Verification reports identify backend and isolation status.
