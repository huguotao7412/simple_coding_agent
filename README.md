# Simple Coding Agent

A local coding-agent prototype built around Planner-Actor orchestration, isolated git worktrees, MCP-backed tools, context management, and fixture-based evaluations.

The project is intentionally small enough to inspect, but it exercises the same concerns that matter in larger autonomous coding systems: task decomposition, isolated execution, tool safety, state tracking, patch merging, and measurable regression tests.

## Current Status

This repository is an engineering MVP, not a finished product. The core architecture is in place and the first eval harness exists. The next milestones are structured Actor summaries, stronger Planner merge/verify loops, and a larger eval suite.

## Architecture

```text
User request
  -> Planner
      - decomposes work
      - updates GlobalState
      - delegates independent tasks
      - synthesizes results
  -> Actor workers
      - run in isolated git worktrees
      - use role-specific tool access
      - call MCP-backed filesystem and shell tools
  -> Planner
      - receives summaries and diffs
      - applies patches
      - reports final result
```

Key modules:

- `core/planner.py`: top-level orchestration loop.
- `core/agent.py`: Actor ReAct execution loop.
- `core/state.py`: shared task ledger and change log.
- `core/tools/delegate.py`: concurrent Actor dispatch with dependency handling.
- `core/git_utils.py`: worktree setup, diff extraction, and cleanup.
- `core/mcp/client.py`: MCP server lifecycle, tool routing, timeout handling, and circuit breaker.
- `evals/run_evals.py`: fixture-based eval runner.
- `cli/main.py`: command-line entry point.
- `web/main.py`: Streamlit UI entry point.

## Features

- Planner-Actor task orchestration.
- Concurrent Actor execution with a configurable maximum actor count.
- Per-Actor git worktree isolation.
- MCP tool integration for filesystem and shell operations.
- Role-based tool allowlists for scout, coder, and verifier Actors.
- Global task state ledger with snapshots and change records.
- Context compression and large tool-result truncation.
- Patch extraction and application support.
- CLI, non-interactive CLI, and Streamlit UI surfaces.
- Fixture-based eval runner with JSON and Markdown reports.

## Quick Start

Requirements:

- Python 3.12+
- Node.js and npm, for MCP server dependencies
- Git

Install Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
```

Install MCP server dependencies:

```powershell
npm install
```

Create local configuration:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `SCA_API_KEY`.

Run the CLI:

```powershell
.\.venv\Scripts\python.exe -m cli.main --workspace . --prompt "Inspect this repository and summarize the architecture."
```

Run the Streamlit UI:

```powershell
.\.venv\Scripts\sca-web
```

## Configuration

Environment variables:

- `SCA_API_KEY`: API key for the OpenAI-compatible model provider.
- `SCA_API_BASE`: API base URL. Defaults to `https://api.deepseek.com`.
- `SCA_MODEL`: model name. Defaults to `deepseek-v4-pro`.
- `SCA_MAX_TOKENS`: model context/token budget. Defaults to `128000`.
- `SCA_WORKSPACE`: workspace shown by the web UI. Defaults to `./workspaces`.
- `SCA_MAX_ACTORS`: maximum concurrent Actors. Defaults to `4`.

## Testing

Run unit tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The eval fixture repositories under `evals/cases/*/repo` intentionally contain failing or incomplete sample projects. They are excluded from normal pytest collection and are executed through the eval runner instead.

Run eval discovery and report generation without calling the agent:

```powershell
.\.venv\Scripts\python.exe -m evals.run_evals --dry-run
```

Run one eval with an agent command template:

```powershell
.\.venv\Scripts\python.exe -m evals.run_evals --case fix_failing_pytest --agent-command ".\.venv\Scripts\python.exe -m cli.main --workspace {workspace} --prompt ""{prompt}"""
```

Reports are written to:

- `evals/reports/latest.json`
- `evals/reports/latest.md`

## Safety Model

The project uses several guardrails:

- Actor changes happen in disposable git worktrees before being merged.
- Filesystem access is bound to the Actor worktree through MCP server configuration.
- Additional path validation rejects absolute paths outside the active worktree.
- Tool calls have timeouts and a provider-level circuit breaker.
- Context storage truncates large tool results to reduce runaway token usage.

These guardrails reduce risk, but they are not a complete sandbox. Run the agent only against repositories and environments you are comfortable modifying.

## Roadmap

- Structured Actor JSON summaries.
- Deterministic Planner merge, verify, and retry protocol.
- More eval cases with pass-rate tracking.
- Trace persistence and replay.
- CI-published eval reports.
- Stronger command policy and audit logging.

## License

MIT. See `LICENSE`.
