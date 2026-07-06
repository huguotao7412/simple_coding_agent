"""Integration tests for the role configuration system."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_all_roles_have_valid_config():
    """Every ActorRole must have a corresponding RoleConfig."""
    from core.role_config import ActorRole, get_role_config

    for role in ActorRole:
        cfg = get_role_config(role)
        assert cfg.system_prompt, f"{role} has empty system_prompt"
        assert cfg.default_max_steps > 0, f"{role} has non-positive max_steps"
        # tool_allowlist can be None (all tools) or a non-empty set
        if cfg.tool_allowlist is not None:
            assert len(cfg.tool_allowlist) > 0, f"{role} has empty tool_allowlist"


def test_scout_is_read_only():
    """Scout must NOT have write/edit/bash in its allowlist."""
    from core.role_config import ActorRole, get_role_config

    scout_cfg = get_role_config(ActorRole.SCOUT)
    assert scout_cfg.tool_allowlist is not None, "Scout must have explicit tool allowlist"
    forbidden = {"write", "edit", "bash", "write_file", "edit_file", "run"}
    assert not (scout_cfg.tool_allowlist & forbidden), \
        f"Scout allowlist contains forbidden tools: {scout_cfg.tool_allowlist & forbidden}"


def test_verifier_has_bash():
    """Verifier must have bash for running tests."""
    from core.role_config import ActorRole, get_role_config

    verifier_cfg = get_role_config(ActorRole.VERIFIER)
    assert verifier_cfg.tool_allowlist is not None, "Verifier must have explicit tool allowlist"
    assert "run" in verifier_cfg.tool_allowlist, "Verifier needs bash-mcp run to run tests"
    assert "read_file" in verifier_cfg.tool_allowlist, "Verifier needs read_file to inspect code"


def test_coder_has_full_access():
    """Coder should have no tool restrictions (allowlist=None)."""
    from core.role_config import ActorRole, get_role_config

    coder_cfg = get_role_config(ActorRole.CODER)
    assert coder_cfg.tool_allowlist is None, "Coder must have full tool access"


def test_tasknode_supports_verifying_status():
    """TaskNode must accept 'verifying' as a valid status."""
    from core.state import TaskNode

    task = TaskNode(task_id="test", description="test", status="verifying")
    assert task.status == "verifying"


def test_actor_accepts_max_steps():
    """ActorAgent must accept and store max_steps."""
    from core.agent import ActorAgent
    import inspect
    sig = inspect.signature(ActorAgent.__init__)
    assert "max_steps" in sig.parameters, "ActorAgent.__init__ missing max_steps parameter"


def test_planner_accepts_max_steps():
    """Planner must accept and store max_steps."""
    from core.planner import Planner
    import inspect
    sig = inspect.signature(Planner.__init__)
    assert "max_steps" in sig.parameters, "Planner.__init__ missing max_steps parameter"


def test_prompts_are_non_empty():
    """All system prompts must be non-empty strings."""
    from core.system_prompt import (
        PLANNER_SYSTEM_PROMPT, ACTOR_SYSTEM_PROMPT,
        SCOUT_SYSTEM_PROMPT, VERIFIER_SYSTEM_PROMPT,
    )
    assert len(PLANNER_SYSTEM_PROMPT) > 500
    assert len(ACTOR_SYSTEM_PROMPT) > 500
    assert len(SCOUT_SYSTEM_PROMPT) > 500
    assert len(VERIFIER_SYSTEM_PROMPT) > 500


def test_delegate_schema_has_role_field():
    """DelegateTool schema must expose role and max_steps fields."""
    from core.tools.delegate import DelegateTool
    props = DelegateTool.parameters["subtasks"]["items"]["properties"]
    assert "role" in props
    assert "max_steps" in props
    assert props["role"]["enum"] == ["scout", "coder", "verifier"]


def test_mcp_provider_exposes_local_actor_tools():
    """MCPToolProvider must expose local code-intelligence tools to Actors."""
    from core.mcp.client import MCPToolProvider

    provider = MCPToolProvider()
    local_names = set(provider._local_tools)
    assert {"list_dir", "read_outline", "search_codebase"} <= local_names
