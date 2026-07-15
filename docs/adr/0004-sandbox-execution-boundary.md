# ADR-0004: Remote sandbox execution boundary

Status: Accepted

## Context

Git worktrees isolate version-control state, not operating-system authority. Local
shell and verification processes inherit the host user's filesystem and network
permissions. Requiring Docker Desktop also creates disproportionate onboarding cost
for a Python CLI, especially on Windows.

## Decision

Introduce a `SandboxBackend` port with `local` and `e2b` implementations.

- `local` is an explicit, non-isolated compatibility mode.
- `e2b` is the production isolation mode and never falls back to local execution.
- Actor shell and deterministic verification share the selected backend.
- Host code owns worktree creation, dependency baselines, diff extraction, merge,
  and cleanup.
- A bounded ZIP transport synchronizes the current Actor worktree around every E2B
  command. Paths are validated on both transfer directions.
- `.env`, `.git`, `.sca`, virtual environments, dependency caches, symlinks, and
  common credential files never cross the boundary.
- E2B outbound internet access is disabled by default.
- Each Actor worktree owns a persistent E2B session, which is killed before the host
  worktree is removed.

## Trust boundary

Trusted host code includes sandbox configuration, archive validation, worktree and
Git lifecycle, and final patch handling. Actor-authored shell strings, test/build
commands, and their subprocesses run remotely. E2B and its cloud control plane are
trusted infrastructure; repository files eligible for transfer leave the host.

## Consequences

- End users need only Python dependencies and an E2B API key, not Docker or WSL.
- Filesystem MCP remains host-scoped. A host edit becomes visible remotely before
  the next command, and remote command changes are applied back afterward.
- Full archive synchronization is intentionally simple and auditable but adds
  latency. A manifest/delta transport may replace it behind the same boundary.
- Projects larger than `SCA_SANDBOX_MAX_TRANSFER` fail closed.
- Network-dependent commands require explicit `SCA_E2B_ALLOW_INTERNET=true`.

## Rejected alternatives

- Python `venv`, subprocess restrictions, and RestrictedPython: not OS boundaries.
- Docker as the only production backend: strong local workflow, excessive onboarding.
- WASI as the general backend: insufficient compatibility for Git, pip, npm, and
  arbitrary project toolchains.
- Silent E2B-to-local fallback: violates the operator's security expectation.
