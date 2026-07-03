from .search import SearchCodebaseTool
from .list_dir import ListDirTool
from .read_outline import ReadOutlineTool
from .update_state import UpdateStateTool
from .delegate import DelegateTool
from .apply_patch import ApplyPatchTool

# Actor tools (execution layer)
# Note: read/write/edit/bash have been migrated to MCP Servers
# (@modelcontextprotocol/server-filesystem + bash-mcp).
# These 3 tools remain as they have no direct MCP equivalent yet:
ACTOR_TOOLS = [
    SearchCodebaseTool,
    ReadOutlineTool,
    ListDirTool,
]

# Planner tools (orchestration layer) — tools that schedule and observe
PLANNER_TOOLS = [
    UpdateStateTool,
    DelegateTool,
    ApplyPatchTool,
    ListDirTool,
    SearchCodebaseTool,
    ReadOutlineTool,
]

__all__ = [
    "SearchCodebaseTool", "ListDirTool", "ReadOutlineTool",
    "UpdateStateTool", "DelegateTool", "ApplyPatchTool",
    "ACTOR_TOOLS", "PLANNER_TOOLS",
]
