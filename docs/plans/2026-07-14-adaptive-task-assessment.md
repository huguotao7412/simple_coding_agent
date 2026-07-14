# Adaptive Task Assessment

## Goal

Introduce a deterministic, measurable decision point before Planner execution so
that Simple Coding Agent can stop treating every code change as the same workflow.
The first increment recommends an execution strategy; later increments will enforce
budgets and calibrate the rules against repeated eval runs.

## Why now

The runtime already has isolated Actors, durable runs, deterministic verification,
traces, and aggregate eval metrics. The current Planner prompt nevertheless requires
a Scout for unfamiliar repositories and a Coder/Verifier pair for every code change.
That is safe as a default, but it creates avoidable model calls for small tasks and
does not expose why a particular orchestration path was selected.

## P1: versioned deterministic assessment (this change)

1. Add a typed `TaskAssessment` contract with:
   - intent, complexity, and risk classifications;
   - a recommended execution strategy;
   - repository and prompt signals used by the decision;
   - bounded execution hints and human-readable reasons.
2. Build a bounded, read-only workspace profile without calling an LLM.
3. Assess every new Planner turn before its first model call.
4. Inject the assessment as a system message so durable checkpoints retain it.
5. Publish a `task_assessment` event and include it in final reports, JSONL traces,
   and aggregate eval results.
6. Keep the assessment advisory in P1. The Planner may override it when repository
   evidence contradicts the initial signals, but must treat it as the default.

## P2: policy enforcement and budgets

1. Add an `ExecutionPlan` derived from the assessment.
2. Enforce actor-count, model-call, repair-attempt, token, and wall-clock budgets.
3. Record explicit strategy overrides with old/new strategy and reason.
4. Prefer repository quality gates over a model Verifier for low-risk tasks.
5. Add cancellation and terminal failure categories at run and task-attempt level.

## P3: eval calibration

1. Expand eval fixtures across task intent, scope, risk, and repository size.
2. Run each real-model task at least three times.
3. Compare pass rate, first-pass verification rate, p50/p95 duration, tokens,
   model/tool calls, repairs, and policy denials by recommended strategy.
4. Tune deterministic thresholds only from checked-in baseline evidence.
5. Add regression thresholds: no pass-rate regression, at least 30% median token
   reduction and 25% median duration reduction for small tasks.

## Decision rules in P1

- Read-only requests use `planner_direct`.
- High-risk or large-scope changes use `scout_then_dag`.
- Medium-scope changes use `scout_then_coder` unless explicit file targets make
  exploration unnecessary.
- Small changes with configured quality gates use `coder_with_gates`.
- Other small changes use `single_actor`.

The rules intentionally use inspectable signals rather than a second model call.
Assessment schema versioning allows later changes without making old traces
ambiguous.

## Non-goals

- P1 does not bypass worktree isolation or tool policy.
- P1 does not claim that lexical risk detection is a security boundary.
- P1 does not automatically execute dangerous operations.
- P1 does not replace deterministic project verification with model judgment.

## Acceptance criteria

- Assessment is deterministic for the same prompt and workspace profile.
- Workspace inspection is bounded and ignores generated/vendor directories.
- The first Planner model payload contains the assessment.
- Non-interactive checkpoints retain the assessment message.
- Final reports and eval result JSON expose the selected strategy.
- Existing runtime, persistence, verification, and eval tests remain green.
