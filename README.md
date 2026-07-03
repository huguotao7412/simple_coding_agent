<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/English-🇬🇧-white?style=for-the-badge" alt="English"></a>
  <a href="README_CN.md"><img src="https://img.shields.io/badge/中文-🇨🇳-red?style=for-the-badge" alt="中文"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/Architecture-Planner--Actor-8A2BE2?style=for-the-badge" alt="Planner-Actor">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="License MIT">
  <img src="https://img.shields.io/badge/Model-DeepSeek%20V4%20Pro-4B8BF5?style=for-the-badge" alt="DeepSeek V4 Pro">
</p>

<h1 align="center">🧠 Simple Coding Agent</h1>

<p align="center">
  <b>A Production-Grade Plan-and-Execute Agent for Autonomous Software Engineering</b>
</p>

<p align="center">
  <i>"Don't tell the agent what to type — tell it what you want, and watch it figure out the rest."</i>
</p>

---

> **🎯 TL;DR** — SCA is an AI coding agent that decomposes complex engineering tasks into concurrent subtasks, dispatches them to isolated worker agents, and synthesizes the results — all while maintaining a real-time global state ledger. Think of it as a **miniature CI/CD pipeline driven by an LLM brain**.

---

## 📖 Table of Contents

- [Why Another Coding Agent?](#-why-another-coding-agent)
- [Architecture Overview](#-architecture-overview)
- [Core Features](#-core-features)
- [Project Structure](#-project-structure)
- [Quick Start](#-quick-start)
- [Configuration Reference](#-configuration-reference)
- [Tool Arsenal](#-tool-arsenal)
- [Dual Interfaces](#-dual-interfaces)
- [Safety & Guardrails](#-safety--guardrails)
- [Advanced Usage](#-advanced-usage)
- [FAQ](#-faq)
- [Roadmap](#-roadmap)

---

## 🤔 Why Another Coding Agent?

Most coding agents are **single-threaded**. They think, then act, then think, then act — one step at a time. This works fine for trivial edits, but falls apart on real-world tasks like:

> *"Add authentication to this FastAPI app, write unit tests, and update the OpenAPI spec."*

A single-threaded agent serializes everything — 15 minutes later, you're still waiting. **SCA is different.** It decomposes that request into 3 independent subtasks, dispatches them to 3 concurrent Actor agents, and you're done in **a third of the time**.

| | Traditional Agent | **Simple Coding Agent** |
|---|---|---|
| Task Model | Linear ReAct loop | **Plan → Delegate → Synthesize** |
| Concurrency | 🚫 Serial only | ✅ Up to 4 concurrent Actors |
| State Tracking | Ad-hoc (lost on crash) | ✅ Global state machine with change log |
| Context Mgmt | Naive truncation | ✅ Hierarchical: scratchpad retention + old-message summarization |
| Loop Detection | None or primitive | ✅ Action-hash circuit breaker |
| Tool Safety | Basic | ✅ 3-layer path sandbox + command blacklist + syntax pre-check |

---

## 🏗️ Architecture Overview

```
                        ┌─────────────────────────────┐
                        │         USER INPUT           │
                        └─────────────┬───────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────────┐
                        │        🧠 PLANNER            │
                        │   (Orchestration Agent)      │
                        │                              │
                        │  • Task decomposition        │
                        │  • GlobalState management     │
                        │  • Actor dispatch & synthesis │
                        │  • Context compression        │
                        └──────┬──────────────┬────────┘
                               │              │
                    update_state│              │ delegate()
                               │              │
                               ▼              ▼
                    ┌──────────────┐  ┌──────────────────┐
                    │  GlobalState │  │   ⚡ ACTOR POOL    │
                    │  (Singleton) │  │  (max 4 concurrent)│
                    │              │  ├──────────────────┤
                    │ • TaskTree   │  │ Actor-1: auth     │
                    │ • ChangeLog  │  │ Actor-2: tests    │
                    │ • Snapshots  │  │ Actor-3: docs     │
                    └──────────────┘  └──────┬───────────┘
                                             │ summaries
                                             ▼
                                    ┌──────────────────┐
                                    │   PLANNER syncs   │
                                    │   → Synthesizes   │
                                    │   → Responds      │
                                    └──────────────────┘
```

### The Two-Layer Model

**Layer 1 — Planner (The Brain)**
- Runs a ReAct loop with orchestration-level tools (`delegate`, `update_state`, `search_codebase`, `list_dir`, `read_outline`)
- Never touches files or runs shell commands directly
- Maintains a `GlobalState` singleton — the single source of truth for all tasks
- Handles context compression when approaching token limits

**Layer 2 — Actor Pool (The Hands)**
- Stateless, isolated execution units — each gets a fresh `ContextManager` and MCP-backed tool access via `@modelcontextprotocol/server-filesystem` + `bash-mcp`
- Launched concurrently via `asyncio.Semaphore(4)`
- Returns structured `ActorSummary { task_id, status, files_modified, bugs_found, key_findings }`
- Each Actor runs in a dedicated git worktree with its own MCP Server processes — complete process-level isolation

### GlobalState: The Ledger

```python
# Every task gets a UUID, dependency list, status, and result summary
TaskNode(
    task_id="task_a1b2c3d4",
    description="Add JWT authentication middleware",
    status="running",       # pending → running → done / failed
    dependencies=[],        # block until these complete
    assigned_actor=None,
    result_summary=None,
)
```

The `ChangeLog` records every mutation — add, update, summary — with timestamps. The Planner consumes changes incrementally via `consume_changes()`, so it always knows exactly what happened and when.

---

## ✨ Core Features

### 1. 🚀 Concurrent Task Orchestration

The Planner decomposes a user request into a **dependency-aware task tree**, then dispatches independent subtasks to up to **4 concurrent Actors**. Each Actor runs in its own `ContextManager` sandbox — no shared mutable state, no race conditions.

```text
User: "Refactor the auth module, add rate limiting, and write integration tests"

Planner:
  ├── task_01: Refactor auth.py → Actor-1 (running)
  ├── task_02: Add rate limiter  → Actor-2 (running)  ← concurrent!
  ├── task_03: Write tests       → Actor-3 (running)  ← concurrent!
  └── Synthesize results         → Final response
```

### 2. 🧠 Chain-of-Thought Streaming

DeepSeek V4 Pro's reasoning tokens are streamed in real-time with visual distinction:

```
> 🧠 Thinking...
> Let me analyze the task tree first...
> The user wants three things done, all independent...
> I'll dispatch them concurrently and wait for summaries...

(Then the final answer streams normally)
```

Both CLI and Web UIs render thinking tokens in a visually distinct style — you see **how** the agent reasons, not just **what** it concludes.

### 3. ⚡ Circuit Breaker (Loop Detection)

Agent got stuck calling the same failing tool repeatedly? SCA catches it:

```
Action hash: hash("bash" + json.dumps({"command": "npm run build"}))

[recent_actions]: [hash1, hash2, hash1, hash3, hash1]
                               ↑ count(hash1) >= 2 → BREAKER TRIPS

→ System Alert injected into conversation
→ Agent forced to change strategy
```

No infinite loops. No wasted API credits.

### 4. 🧠 Hierarchical Memory Compression

When context hits **80% of the model's limit**:

| Layer | Strategy | What's Preserved |
|---|---|---|
| **System Prompt** | Frozen — never touched | Agent identity, rules, tool schemas |
| **Scratchpad** (work log) | Extracted & **retained verbatim** | Completed tasks, active bugs, key file paths |
| **Middle history** | LLM-summarized | Key decisions, file modifications |
| **Recent messages** | Kept as-is (last N turns) | Immediate conversational context |

This "hard retention + soft summarization" design means the agent **never forgets what it's currently working on**, even under extreme context pressure.

### 5. 🛡️ Safe by Default

- **Path Sandbox**: Every file operation is validated — `os.path.realpath()` check prevents `../../../etc/passwd` escapes
- **Command Blacklist**: `sudo`, `rm -rf /`, `mkfs`, `dd if=`, fork bombs, and raw device writes are regex-blocked
- **Syntax Pre-check**: `write` and `edit` validate Python/JSON syntax **before** touching disk — broken code is rejected upfront
- **Env Hardening**: Shell sessions run with `DEBIAN_FRONTEND=noninteractive`, `CI=1`, `GIT_TERMINAL_PROMPT=0` — no hangs waiting for user input

---

## 📁 Project Structure

```
simple_coding_agent/
│
├── pyproject.toml                # Package config, entry points (sca / sca-web), deps
├── package.json                  # Node.js MCP Server dependencies
├── .env.example                  # Environment variable template
├── .gitignore
│
├── core/                         # 🧠 BRAIN LAYER — zero UI coupling
│   ├── planner.py                # Planner agent: decompose → delegate → synthesize
│   ├── agent.py                  # ActorAgent: isolated ReAct executor
│   ├── state.py                  # GlobalState: task tree + change log (singleton)
│   ├── context.py                # ContextManager: token estimation, hierarchical compression
│   ├── llm.py                    # LLMClient: async OpenAI-compatible streaming with retry
│   ├── system_prompt.py          # Planner & Actor system prompts
│   ├── exceptions.py             # Exception hierarchy (SCAAgentError → LLMAPIError, etc.)
│   │
│   ├── mcp/                      # 🔌 MCP (Model Context Protocol) integration
│   │   ├── __init__.py           # Package exports
│   │   └── client.py             # MCPToolProvider: dual-server lifecycle, schema conversion
│   │
│   └── tools/                    # 🛠️ Tool implementations
│       ├── base.py               # BaseTool ABC, ToolResult dataclass, semantic_truncate()
│       ├── delegate.py           # Concurrent Actor dispatch (asyncio.Semaphore gate)
│       ├── update_state.py       # GlobalState CRUD (add_task / update_task / add_summary)
│       ├── apply_patch.py        # Patch application with fuzz matching + .rej cleanup
│       ├── search_codebase.py    # Dual-mode: AST symbol search + regex text search
│       ├── list_dir.py           # Directory listing with emoji icons
│       ├── read_outline.py       # File skeleton viewer (AST for .py, regex for others)
│       └── __init__.py           # ACTOR_TOOLS & PLANNER_TOOLS registries
│
├── cli/                          # 🖥️ TERMINAL SKIN
│   ├── main.py                   # CLI entry point (sca command), lazy imports
│   ├── ui.py                     # Rich-based live Markdown rendering, tool status cards
│   └── bridge.py                 # async event loop → Rich UI bridge
│
└── web/                          # 🌐 WEB SKIN (Streamlit)
    ├── cli.py                    # sca-web entry point (wraps streamlit run)
    ├── main.py                   # 3-column layout: sidebar | chat | file preview
    ├── bridge.py                 # Threaded async event → Streamlit session_state bridge
    └── components/
        ├── sidebar.py            # Project switcher + file tree + task board
        ├── chat.py               # Chat history + streaming event renderer
        └── diff.py               # HTML diff renderer (green/red)
```

### Tool Assignment

| Tool | Planner | Actor | Source | Purpose |
|---|---|---|---|---|
| `delegate` | ✅ | ❌ | Local | Dispatch subtasks to concurrent Actors |
| `update_state` | ✅ | ❌ | Local | CRUD operations on the global task tree |
| `apply_patch` | ✅ | ❌ | Local | Apply Actor diffs back to main workspace |
| `read_file` | ❌ | ✅ | MCP | Chunked file reading with line numbers |
| `write_file` | ❌ | ✅ | MCP | Full file creation/overwrite |
| `edit_file` | ❌ | ✅ | MCP | Precision search-replace with dry-run preview |
| `run` | ❌ | ✅ | MCP | Shell command execution with timeout |
| `run_background` | ❌ | ✅ | MCP | Start background processes (dev servers, etc.) |
| `search_files` | ❌ | ✅ | MCP | Glob-based file search |
| `search_codebase` | ✅ | ✅ | Local | AST symbol lookup + regex text search |
| `list_dir` / `list_directory` | ✅ | ✅ | Local/MCP | Directory listing |
| `read_outline` | ✅ | ✅ | Local | File skeleton — signatures only, no body |

> **Design principle**: The Planner *observes and decides*; Actors *execute and report*. Actor file/shell tools are now provided by community MCP Servers (`@modelcontextprotocol/server-filesystem` + `bash-mcp`), enabling process-level isolation and ecosystem compatibility.

---

## 🚀 Quick Start

### Prerequisites

| Requirement | Version | Why |
|---|---|---|
| **Python** | ≥ 3.12 | Native `asyncio` improvements, AST features |
| **Node.js** | ≥ 18 | MCP Server runtime (filesystem + bash) |
| **API Key** | DeepSeek (or OpenAI-compatible) | The LLM brain |
| **Git** | Any recent version | For `git diff` safety net |

### 1. Clone & Install

```bash
git clone https://github.com/huguotao7412/simple_coding_agent.git
cd simple_coding_agent

# Create venv + install (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

# Optional: dev dependencies (pytest)
pip install -e ".[dev]"
```

### 2. Configure Environment

Create a `.env` file in the project root:

```bash
# Required
SCA_API_KEY=sk-your-deepseek-api-key-here

# Optional — defaults shown
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_WORKSPACE=./workspaces
```

### 3. Launch

```bash
# Terminal mode — interactive REPL
sca

# Specify a workspace directory
sca --dir /path/to/your/project

# Override model for one session
sca --model gpt-4o
```

```bash
# Web dashboard — IDE-like experience
sca-web
# → Opens http://localhost:8501
```

### 4. Try It Out

Once the REPL is running, just talk to it naturally:

```
> Initialize a FastAPI project with a /health endpoint and Dockerfile.

> Read main.py, find the bubble sort, and replace it with quicksort.

> Run pytest. If anything fails, read the error, fix the code, and re-run until all green.

> Find all places where we're using os.path and migrate them to pathlib.

> Add JWT authentication to this Flask app. Write tests. Update the README.
```

Type `exit` or `quit` to leave. Press `Ctrl+C` to interrupt a running agent.

---

## ⚙️ Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SCA_API_KEY` | ✅ Yes | — | Your API key (DeepSeek or OpenAI-compatible) |
| `SCA_API_BASE` | No | `https://api.deepseek.com` | API endpoint URL |
| `SCA_MODEL` | No | `deepseek-v4-pro` | Model identifier |
| `SCA_MAX_TOKENS` | No | `128000` | Token budget per API call |
| `SCA_WORKSPACE` | No | `./workspaces` | Root directory for project workspaces (Web mode) |

---

## 🔌 MCP Integration (Model Context Protocol)

SCA embraces the open-source MCP ecosystem. Actor agents no longer run local tool code — instead, each Actor spawns dedicated MCP Server subprocesses:

```
Actor (isolated git worktree)
  ├── @modelcontextprotocol/server-filesystem  →  read_file, write_file, edit_file,
  │                                                search_files, list_directory, ...
  └── bash-mcp                                 →  run, run_background, kill_background,
                                                   list_background
```

**Why MCP?**
- **Process isolation** — tool crashes don't affect the Actor's LLM loop
- **Ecosystem leverage** — new capabilities (databases, APIs, browsers) are just `npm install` away
- **Zero maintenance** — file I/O and shell execution are maintained by the MCP community
- **Future-proof** — any MCP-compatible server plugs in with zero code changes

**Adding a new MCP Server** in the future is as simple as adding it to `core/mcp/client.py`'s server list.

```bash
# Install MCP Server dependencies (one-time)
npm install
# Or globally:
npm install -g @modelcontextprotocol/server-filesystem bash-mcp
```

---

## 🛠️ Tool Arsenal

### `read_file` (MCP) — File Reader
```
read_file(path="src/auth.py")
→ Returns complete file contents with UTF-8 encoding.
```

### `write_file` (MCP) — File Writer
```
write_file(path="src/new_module.py", content="...")
→ Creates or overwrites a file. Creates parent directories automatically.
```

### `edit_file` (MCP) — Smart Diff Editor
```
edit_file(path="src/auth.py", edits=[{"oldText": "...", "newText": "..."}])
→ Line-based selective editing with dry-run preview mode.
  Returns Git-style diff output.
```

### `run` (MCP) — Shell Command
```
# Execute a command
run(command="pytest tests/ -v")

# Execute with timeout and working directory
run(command="npm test", options={"cwd": "/path/to/project", "timeout": 60000})
```

### `run_background` (MCP) — Background Process
```
# Start a dev server
run_background(command="uvicorn app:app --port 8000", name="backend")
→ Returns PID. Use kill_background("backend") to stop.
```

### `search_codebase` — Dual-Mode Search
```
# AST symbol search (Python classes/functions with signatures + docstrings)
search_codebase(query="authenticate", mode="symbol")

# Regex text search with 2-line context window
search_codebase(query="TODO|FIXME|HACK", mode="text")

# Filter by extension
search_codebase(query="def test_", mode="symbol", include_ext=".py")
```

### `read_outline` — File Skeleton
```
read_outline(file_path="core/agent.py")
→ Returns:
  L   25     [Class]  class ActorAgent:
  L  125      [Func]  def run(self, user_input, on_token=None) -> ActorSummary:
  L  251      [Func]  async def run_stream(self, user_input) -> AsyncGenerator:
  ...
→ Use this FIRST on large files, then read specific sections.
```

### `list_dir` — Directory Explorer
```
list_dir(dir_path="core/tools")
→ Returns tree-like listing with 📁/📄 icons, ignoring .git, .venv, etc.
```

### Planner-Only Tools

### `delegate` — Concurrent Actor Dispatch
```
delegate(subtasks=[
  {"task_id": "task_01", "description": "Add JWT middleware",
   "context_files": ["src/auth.py"], "context_summaries": ["..."]},
  {"task_id": "task_02", "description": "Write tests",
   "context_files": ["tests/test_auth.py"]},
])
→ Launches up to 4 Actors concurrently via asyncio.Semaphore
→ Returns: structured summaries for each subtask
```

### `update_state` — Global Ledger CRUD
```
update_state(action="add_task", description="Refactor auth module")
update_state(action="update_task", task_id="task_01", status="running")
update_state(action="add_summary", task_id="task_01", summary="Done. Modified 3 files.")
```

---

## 🖥️ Dual Interfaces

### CLI (`sca`)
<p>
  <b>Rich-powered terminal experience</b> — live Markdown streaming, tool execution spinners, and DeepSeek reasoning chain display.
</p>

- Streaming Markdown via `rich.Live` with cursor animation
- Real-time tool status cards: ⚡ running (cyan) → ✅ done / ❌ failed
- DeepSeek thinking tokens rendered as `> 🧠 Thinking...` blockquotes
- SCA ASCII art logo on launch (because why not)
- `Ctrl+C` to interrupt, `exit` to quit

### Web (`sca-web`)
<p>
  <b>Streamlit IDE-like dashboard</b> — project switcher, file tree, task board, and chat in one view.
</p>

| Panel | What It Shows |
|---|---|
| **Sidebar** | Project dropdown switcher, expandable file tree, real-time task status board |
| **Main Chat** | Streaming agent responses, collapsible tool execution cards, diff-colored edit results |
| **File Preview** | Click any file in sidebar → syntax-highlighted preview with line numbers |
| **Task Board** | Live `GlobalState` snapshot — see which tasks are pending/running/done/failed |

---

## 🛡️ Safety & Guardrails

SCA can write files and execute shell commands. We take safety seriously:

### Defense in Depth

```
Layer 1 — Path Sandbox
  All file ops go through BaseTool.validate_path() →
  os.path.realpath() check against workspace root.
  ../../../etc/passwd → BLOCKED

Layer 2 — Command Blacklist
  sudo, rm -rf /, mkfs, dd if=, chmod 777 /, fork bombs,
  format C:, > /dev/sda → all regex-blocked

Layer 3 — Syntax Pre-check
  write & edit parse Python AST / JSON before touching disk.
  SyntaxError → Edit Rejected, file untouched.

Layer 4 — Loop Circuit Breaker
  Same tool + same args repeated ≥2 times in recent history →
  execution skipped, System Alert injected

Layer 5 — Environment Hardening
  DEBIAN_FRONTEND=noninteractive, CI=1, GIT_TERMINAL_PROMPT=0
  → prevents apt/npm/git from blocking on prompts
```

### Recommended Practices

1. **Always use version control.** Run SCA inside a git repo. Review every change with `git diff` before committing. If something goes wrong: `git reset --hard`.

2. **Never run as root.** No `sudo sca`. The agent doesn't need root, and neither should you.

3. **Use Docker for untrusted tasks.** Mount your workspace into a container for an extra isolation layer.

4. **Review the diff.** The `edit` tool returns a unified diff for every change. Skim it before moving on.

---

## 🔧 Advanced Usage

### Switching Between CLI and Web

Both interfaces share the same `core/` engine. You can:
- Start a task in `sca-web`, review the task board, then continue in `sca`
- Run multiple `sca` sessions against different workspaces simultaneously
- The Web UI persists session state across page refreshes

### Custom System Prompts

The system prompts live in `core/system_prompt.py`. Want a different agent personality?

```python
# Edit PLANNER_SYSTEM_PROMPT or ACTOR_SYSTEM_PROMPT
# No config files needed — just Python strings
```

### Adding a New Tool

```python
# 1. Create core/tools/my_tool.py
from .base import BaseTool, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something useful."
    parameters = {...}
    required_params = [...]

    async def execute(self, **kwargs) -> ToolResult:
        ...

# 2. Register in core/tools/__init__.py
from .my_tool import MyTool

# Add to ACTOR_TOOLS, PLANNER_TOOLS, or both depending on access level
ACTOR_TOOLS = [..., MyTool]
```

### Programmatic API

```python
import asyncio
from core.llm import LLMClient
from core.context import ContextManager
from core.planner import Planner
from core.tools import PLANNER_TOOLS
from core.system_prompt import PLANNER_SYSTEM_PROMPT

async def main():
    llm = LLMClient(api_key="...", model="deepseek-v4-pro")
    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    planner = Planner(llm, ctx, tools, workspace_dir="./my_project")

    async for event in planner.run_stream("Add type hints to all functions"):
        if event.type == "thought":
            print(event.token, end="", flush=True)
        elif event.type == "done":
            print(f"\n\nFinal: {event.content}")

asyncio.run(main())
```

---

## ❓ FAQ

<details>
<summary><b>Q: Why DeepSeek? Can I use OpenAI/GPT-4?</b></summary>

SCA is built on the OpenAI-compatible API format. Any model that supports `tools` and streaming will work:

```bash
SCA_API_BASE=https://api.openai.com/v1
SCA_MODEL=gpt-4o
```

However, DeepSeek V4 Pro is **strongly recommended** — its native reasoning tokens (`reasoning_content`) give SCA its Chain-of-Thought transparency. Other models work but won't show the thinking process.
</details>

<details>
<summary><b>Q: How is this different from Claude Code / Aider / Cursor?</b></summary>

- **Claude Code / Cursor** are interactive pair-programming tools. SCA is an **autonomous agent** — you give it a goal and it figures out the rest.
- **Aider** is single-threaded edit-edit-edit. SCA plans first, then executes **concurrently**.
- SCA's **Planner-Actor split** means it can work on multiple files simultaneously — most other agents are strictly sequential.
</details>

<details>
<summary><b>Q: What's the max task complexity SCA can handle?</b></summary>

Practically: anything that fits in 3-5 independent subtasks. The Planner has a 50-step limit, and each Actor has 30 steps. Context compression kicks in at 80% token usage. For truly massive projects, break the work into multiple SCA sessions.
</details>

<details>
<summary><b>Q: Can I run SCA headless / in CI?</b></summary>

Not yet — the CLI currently requires an interactive terminal. Programmatic API is available (see above) but not yet packaged as a CI-friendly command. This is on the roadmap.
</details>

<details>
<summary><b>Q: Does it support Windows?</b></summary>

Yes! SCA is tested on Windows 11. The `bash` tool auto-detects the platform and uses `cmd.exe` persistent sessions on Windows vs `/bin/bash` on Unix. The workspace tree fallback uses pure Python to avoid depending on the `tree` command.
</details>

---

## 🗺️ Roadmap

| Milestone | Status |
|---|---|
| Planner-Actor architecture | ✅ Done |
| GlobalState with dependency DAG | ✅ Done |
| Concurrent Actor dispatch (asyncio gate) | ✅ Done |
| Hierarchical memory compression | ✅ Done |
| Circuit breaker / loop detection | ✅ Done |
| Dual UI (CLI + Streamlit Web) | ✅ Done |
| 8 core tools with syntax validation | ✅ Done |
| Git worktree isolation per Actor | ✅ Done |
| MCP (Model Context Protocol) integration | ✅ Done |
| CI/CD headless mode | 📋 Planned |
| Human-in-the-loop approval for destructive ops | 📋 Planned |
| Persistent session history (SQLite) | 📋 Planned |
| Multi-model routing (cheap model for simple tasks) | 💡 Ideas |

---

## 🙏 Acknowledgments

Built with:
- [DeepSeek](https://deepseek.com) — the LLM that makes this possible
- [Rich](https://github.com/Textualize/rich) — beautiful terminal rendering
- [Streamlit](https://streamlit.io) — rapid web UI prototyping
- [httpx](https://www.python-httpx.org/) — async HTTP with HTTP/2

---

<p align="center">
  <b>⭐ If this project helps you, consider giving it a star!</b><br>
  <sub>Built with ☕ and late nights by <a href="https://github.com/huguotao7412">huguotao7412</a></sub>
</p>
