"""Minimal integration test — verifies the full chain initializes without errors."""
import asyncio
import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_global_state_singleton():
    from core.state import GlobalState
    GlobalState.reset()
    s1 = GlobalState.get()
    s2 = GlobalState.get()
    assert s1 is s2


def test_state_add_and_update():
    from core.state import GlobalState
    GlobalState.reset()
    state = GlobalState.get()
    tid = state.add_task("test task")
    assert tid.startswith("task_")
    assert state.task_tree[tid].status == "pending"

    state.update_task(tid, status="running")
    assert state.task_tree[tid].status == "running"

    state.add_summary(tid, "all done")
    assert state.task_tree[tid].result_summary == "all done"
    assert state.task_tree[tid].status == "running"  # add_summary does NOT auto-set done

    assert len(state.change_log) == 3


def test_update_state_tool():
    from core.tools.update_state import UpdateStateTool
    from core.state import GlobalState
    GlobalState.reset()

    async def _test():
        tool = UpdateStateTool()

        # add_task
        r = await tool.execute(action="add_task", description="test")
        assert r.success
        tid = r.content.split(": ")[1]

        # update_task
        r = await tool.execute(action="update_task", task_id=tid, status="running")
        assert r.success

        # add_summary
        r = await tool.execute(action="add_summary", task_id=tid, summary="done")
        assert r.success

        # error: unknown task
        r = await tool.execute(action="update_task", task_id="bad", status="done")
        assert not r.success
        assert "Unknown" in r.error

    asyncio.run(_test())


def test_planner_initialization():
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.planner import Planner
    from core.tools import PLANNER_TOOLS
    from core.system_prompt import PLANNER_SYSTEM_PROMPT
    from core.state import GlobalState
    GlobalState.reset()

    llm = LLMClient(api_key="test", model="deepseek-v4-pro")
    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    planner = Planner(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=".")

    assert "delegate" in planner.tools_by_name
    assert "update_state" in planner.tools_by_name
    assert "list_dir" in planner.tools_by_name
    # Planner must NOT have write/edit/bash
    assert "write" not in planner.tools_by_name
    assert "edit" not in planner.tools_by_name
    assert "bash" not in planner.tools_by_name


def test_actor_initialization():
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.agent import ActorAgent
    from core.tools import ACTOR_TOOLS
    from core.system_prompt import ACTOR_SYSTEM_PROMPT

    llm = LLMClient(api_key="test", model="deepseek-v4-pro")
    ctx = ContextManager(system_prompt=ACTOR_SYSTEM_PROMPT)
    tools = [t() for t in ACTOR_TOOLS]
    actor = ActorAgent(
        llm_client=llm, context_manager=ctx, tools=tools,
        workspace_dir=".", actor_id="test-1", task_context="test task",
    )

    assert actor.actor_id == "test-1"
    # Actor must have execution tools
    assert "write" in actor.tools_by_name
    assert "bash" in actor.tools_by_name
    # Actor must NOT have delegate/update_state
    assert "delegate" not in actor.tools_by_name
    assert "update_state" not in actor.tools_by_name


def test_semantic_truncate_l0():
    from core.tools.base import semantic_truncate
    text = "hello world"
    result, degraded = semantic_truncate(text, token_budget=100)
    assert not degraded
    assert result == text


def test_semantic_truncate_l1():
    from core.tools.base import semantic_truncate
    text = "x" * 10000
    result, degraded = semantic_truncate(text, file_path="/test/main.py", token_budget=100)
    assert degraded
    assert "read_outline" in result


def test_semantic_truncate_l2():
    from core.tools.base import semantic_truncate
    lines = [f"line {i}" for i in range(1000)]
    lines[500] = "ERROR: critical failure"
    text = "\n".join(lines)
    result, degraded = semantic_truncate(text, token_budget=500)
    assert degraded
    assert "ERROR: critical failure" in result
    assert "lines omitted" in result
