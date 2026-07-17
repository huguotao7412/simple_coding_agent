# Evaluation Layers

Simple Coding Agent uses two deliberately separate evaluation layers.

1. The local fixture suite is a fast product regression and safety smoke suite.
2. Harbor runs externally maintained coding-agent benchmarks in isolated task
   environments.

Do not report the eight local fixtures as a general coding benchmark. They are
small, project-specific checks for runtime regressions, workspace boundaries,
dirty-file preservation, destructive commands, and basic editing behavior.

## Local regression suite

Tasks are defined in `evals/tasks.json`; fixtures live under
`evals/fixtures/<task_id>/`. The check phase is deterministic and offline.

```bash
sca-eval prepare
sca-eval run --model deepseek-v4-pro
sca-eval check
```

Compare aggregate result files with:

```bash
sca-eval compare eval_results.baseline.json eval_results.candidate.json \
  --output eval_comparison.md
```

The local runner writes `eval_results.json`, per-task reports, JSONL traces, and
Actor patches. These artifacts remain useful for debugging project-specific
failures even after Harbor becomes the primary capability benchmark.

## Standard benchmarks with Harbor

Install the optional benchmark dependencies:

```bash
python -m pip install -e ".[benchmark]"
```

Use a Python 3.12 or newer virtual environment for the Harbor runner. The
evaluated SCA wheel creates its own Python 3.12 environment inside every task
container.

Configure the same OpenAI-compatible endpoint used by the normal CLI:

```text
SCA_API_KEY=...
SCA_API_BASE=https://api.deepseek.com
```

Run the default SWE-rebench dataset:

```bash
sca-eval harbor --model deepseek/deepseek-v4-pro
```

Run Terminal-Bench 2.0 instead:

```bash
sca-eval harbor \
  --dataset terminal-bench/terminal-bench-2 \
  --model deepseek/deepseek-v4-pro
```

Arguments after `--` are forwarded to `harbor run`, which can be used for task
selection, job naming, environment selection, or other Harbor options:

```bash
sca-eval harbor --model deepseek/deepseek-v4-pro -- --include-task-name <task-id>
```

The command builds the current checkout into a wheel, uploads that exact wheel
to every Harbor task environment, creates an isolated Python 3.12 virtual
environment, discovers the task repository (`/testbed`, `/app`, `/workspace`, or
`/repo`), and invokes the headless `sca-harbor-agent` entrypoint there.
Use `--wheel path/to/file.whl` to evaluate a previously built release artifact.

If Docker Hub is only reachable through a local proxy, configure Docker
Desktop's Docker Desktop proxy and set its Containers proxy to `Same as host
proxy`. Container setup also needs outbound access to package repositories. Set
standard proxy variables before starting the job; the adapter forwards both
upper- and lower-case forms into the task container:

```powershell
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = $env:HTTP_PROXY
$env:NO_PROXY = "localhost,127.0.0.1"
```

Replace `7890` with the HTTP or mixed proxy port actually exposed by the local
proxy application. The adapter automatically rewrites loopback proxy URLs to
`host.docker.internal` when forwarding them into Linux task containers.

Harbor owns the outer sandbox, so the adapter forces
`SCA_SANDBOX_BACKEND=local` inside the task environment. SCA state and Actor
artifacts are written below `/logs/artifacts/sca`; Harbor collects that directory
after each trial. `run-trace.jsonl` and `sca-run.json` are written to Harbor's
agent log directory, and the latter populates Harbor token and metadata fields.

## Recommended cadence

- Pull requests: local fixture suite.
- Nightly: a pinned 20-30 task SWE-rebench slice.
- Release: 50-100 SWE-rebench tasks, with repeated runs on a stable sentinel
  subset.
- Periodic breadth check: Terminal-Bench 2.0.

Pin dataset versions and task IDs for regression comparisons. Use fresh monthly
SWE-rebench splits separately to measure generalization rather than comparing
their raw scores to an older split.
