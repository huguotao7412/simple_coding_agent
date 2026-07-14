# ADR 0003: Deterministic Verification at the Actor Worktree Boundary

- Status: Accepted
- Date: 2026-07-14

## Context

An LLM Actor can say that a change is complete or that tests pass without producing reproducible evidence. Planner-level review is also semantic and may never execute the repository's real test, type, or lint commands. A production coding agent needs a mechanical acceptance boundary before an implementation diff can flow back into orchestration.

## Decision

`WorktreeActorExecutor` is the acceptance boundary for coder output. Projects may define ordered gates in `.sca/quality-gates.toml`. Each command is an argv array, is launched without a shell, has a timeout, and runs in the Actor's isolated git worktree.

After the initial coder turn:

1. The runtime executes every configured gate and records exit status, duration, timeout state, a compact output excerpt, and a complete log artifact.
2. If all required gates pass, the runtime extracts and exports the diff.
3. If a required gate fails, the runtime gives explicitly delimited evidence to the same Actor context and asks it to repair the implementation.
4. The runtime, not the Actor, reruns the gates. Repair attempts are bounded by configuration, and a repeated normalized failure fingerprint stops the loop early.
5. A still-failing diff is not exported to Planner-visible state.

Missing configuration disables this boundary and preserves the previous execution behavior. Optional gates record evidence but do not block export.

## Alternatives considered

- **Trust the Actor or a Verifier Actor.** This is useful for semantic review but cannot prove that deterministic commands ran or succeeded.
- **Run gates in the primary workspace.** This risks observing or mutating unrelated user changes and breaks the Actor isolation model.
- **Let the Actor choose commands dynamically.** This is flexible but makes acceptance non-reproducible and difficult to audit. Project-owned declarative commands provide an explicit contract.
- **Use an external CI service for every attempt.** This offers stronger infrastructure isolation but adds latency, credentials, and remote coordination. The local boundary remains useful before optional CI publication.

## Consequences

- Acceptance evidence is reproducible and retained under `.sca/artifacts/verification/`.
- Repair consumes a bounded number of additional model turns and process executions.
- Failure fingerprints intentionally ignore timing but include the failed gate, outcome, and compact output; materially identical failures terminate early.
- `shell=False` removes shell parsing ambiguity, but configured executables still run with the current user's OS permissions. The configuration is trusted repository code, not a process sandbox.
- The current runner executes gates sequentially for deterministic ordering and simpler evidence. Safe parallel gates can be considered later if configuration gains explicit dependency semantics.
