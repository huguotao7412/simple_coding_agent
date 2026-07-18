from __future__ import annotations

import json
import re
import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from .conversation import ContextManager
from ..events import AgentEvent
from ..exceptions import LLMAPIError
from ..execution.policy import BudgetExceeded, PolicyViolation
from ..execution.models import ExecutionStrategy
from ..llm import LLMClient
from ..runs.context import RunContext
from ..runs.models import RunStatus
from ..tools.base import BaseTool, ToolResult


EXPLORATION_TOOLS = {"list_dir", "read", "read_outline", "search_codebase"}
ACTOR_EXPLORATION_LIMIT = 8
PLANNER_EXPLORATION_LIMIT = 5
PLANNER_ORCHESTRATION_TOOLS = {"delegate", "update_state"}
MUTATION_TOOLS = {
    "apply_patch",
    "edit",
    "edit_file",
    "write",
    "write_file",
}


WORKSPACE_AWARE_TOOLS = {
    "apply_patch",
    "bash",
    "delegate",
    "edit",
    "list_dir",
    "read",
    "read_outline",
    "search_codebase",
    "update_state",
    "write",
}


@dataclass
class ParsedToolCall:
    tool_name: str
    args: dict[str, Any]
    error: str | None = None


def parse_tool_call(tc: dict[str, Any]) -> ParsedToolCall:
    """Parse an OpenAI-style tool call into a tool name and object args."""
    function = tc.get("function", {})
    tool_name = function.get("name", "")
    raw_args = str(function.get("arguments") or "").strip()
    raw_args = re.sub(r"^\s*```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
    raw_args = re.sub(r"\s*```$", "", raw_args).strip()

    if not raw_args:
        return ParsedToolCall(tool_name=tool_name, args={})

    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return ParsedToolCall(
            tool_name=tool_name,
            args={},
            error=(
                f"Invalid JSON format in arguments: {e}. "
                "Escape newlines as \\n, escape double quotes as \\\" and remove trailing commas."
            ),
        )

    if not isinstance(args, dict):
        args = {}

    return ParsedToolCall(tool_name=tool_name, args=cast(dict[str, Any], args))


class AgentRuntime:
    """Shared ReAct runtime for Planner and Actor-style agents."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool] | None = None,
        workspace_dir: str = "",
        max_steps: int = 30,
        tool_provider: Any | None = None,
        actor_id: str = "",
        dynamic_context_builder: Callable[[], dict[str, Any]] | None = None,
        after_tool_call: Callable[[str, ToolResult], Awaitable[list[AgentEvent]]] | None = None,
        emit_token_stats: bool = False,
        run_context: RunContext | None = None,
    ) -> None:
        self.llm = llm_client
        self.ctx = context_manager
        self.workspace_dir = workspace_dir
        self.max_steps = max_steps
        self.tool_provider = tool_provider
        self.actor_id = actor_id
        self.dynamic_context_builder = dynamic_context_builder
        self.after_tool_call = after_tool_call
        self.emit_token_stats = emit_token_stats
        self.run_context = run_context or RunContext.create()
        self.tools_by_name = {t.name: t for t in tools} if tools else {}
        self._recent_actions: deque[int] = deque(maxlen=10)
        self._outline_reads_by_file: dict[str, int] = {}
        self._available_tool_names: set[str] = set()
        self._exploration_calls_without_mutation = 0
        self._actor_exploration_locked = False
        self._planner_exploration_calls = 0
        self._planner_exploration_locked = False
        self._delegation_calls = 0
        self._successful_mutations = 0
        self._blocked_final_without_mutation = 0
        self.last_result_success = True
        self._terminal_budget_error = ""

    async def _list_tool_schemas(self) -> list[dict[str, Any]]:
        if self.tool_provider is not None:
            schemas = cast(list[dict[str, Any]], await self.tool_provider.list_tools())
        else:
            schemas = [t.schema for t in self.tools_by_name.values()]
        self._available_tool_names = {
            str(schema.get("function", {}).get("name") or schema.get("name") or "")
            for schema in schemas
            if isinstance(schema, dict)
        }
        self._available_tool_names.discard("")
        return schemas

    def _payload_messages(self) -> list[dict[str, Any]]:
        if self.dynamic_context_builder is None:
            return self.ctx.messages
        return self.ctx.messages + [self.dynamic_context_builder()]

    async def _execute_single_tool(
        self,
        tc: dict[str, Any],
    ) -> tuple[str, dict[str, Any], ToolResult, bool]:
        parsed = parse_tool_call(tc)
        tool_name = parsed.tool_name
        args = parsed.args
        tool_call_id = str(tc.get("id", ""))

        cached = (
            self.run_context.completed_tool_calls.get(tool_call_id)
            if self.run_context.store is not None and not self.actor_id
            else None
        )
        if cached is not None:
            result, observation = self._decode_cached_tool_result(cached)
            self.ctx.add_tool_result(tool_call_id, observation)
            return tool_name, args, result, False

        if parsed.error is not None:
            result = ToolResult.fail(parsed.error)
            self.ctx.add_tool_result(tc["id"], f"Error: {parsed.error}")
            return tool_name, args, result, False

        planner_intervention = self._planner_exploration_intervention(tool_name)
        if planner_intervention:
            result = ToolResult.ok(planner_intervention)
            self.ctx.add_tool_result(tool_call_id, planner_intervention)
            return tool_name, args, result, True

        exploration_intervention = self._exploration_loop_intervention(tool_name, args)
        if exploration_intervention:
            result = ToolResult.ok(exploration_intervention)
            self.ctx.add_tool_result(tool_call_id, exploration_intervention)
            return tool_name, args, result, True

        if tool_name in WORKSPACE_AWARE_TOOLS and self.workspace_dir:
            args["workspace_dir"] = self.workspace_dir

        outline_intervention = self._outline_loop_intervention(tool_name, args)
        if outline_intervention:
            result = ToolResult.ok(outline_intervention)
            self.ctx.add_tool_result(tool_call_id, outline_intervention)
            return tool_name, args, result, True

        action_hash = hash(tool_name + json.dumps(args, sort_keys=True))
        if self._recent_actions.count(action_hash) >= 2:
            intervention = "System Alert: Repeated tool call detected. Please try a different approach."
            result = ToolResult.fail(intervention)
            self.ctx.add_tool_result(tc["id"], intervention)
            return tool_name, args, result, True
        self._recent_actions.append(action_hash)

        if self.tool_provider is not None:
            result = await self.tool_provider.call_tool(tool_name, args)
            if result.success:
                observation = result.content
            else:
                observation = f"ERROR: {result.error}"
                if result.content:
                    observation += f"\nPartial output: {result.content}"
            self._record_successful_mutation(tool_name, result)
            self.ctx.add_tool_result(tc["id"], observation)
            return tool_name, args, result, False

        tool = self.tools_by_name.get(tool_name)
        if tool is None:
            result = ToolResult.fail(f"unknown tool '{tool_name}'")
            observation = f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
            self.ctx.add_tool_result(tc["id"], observation)
            return tool_name, args, result, False

        try:
            result = await tool.execute(**args)
        except Exception as e:
            result = ToolResult.fail(f"Internal Tool Error: {str(e)}")

        if result.success:
            observation = result.content
        else:
            observation = f"ERROR: {result.error}"
            if result.content:
                observation += f"\nPartial output: {result.content}"

        self._record_successful_mutation(tool_name, result)
        self.ctx.add_tool_result(tc["id"], observation)
        return tool_name, args, result, False

    def _record_successful_mutation(self, tool_name: str, result: ToolResult) -> None:
        if result.success and tool_name == "delegate":
            self._delegation_calls += 1
            self._planner_exploration_calls = 0
            self._planner_exploration_locked = False
            self._exploration_calls_without_mutation = 0
        if not result.success or tool_name not in MUTATION_TOOLS:
            return
        if tool_name == "apply_patch" and "No changes to apply" in result.content:
            return
        self._successful_mutations += 1
        self._exploration_calls_without_mutation = 0
        self._actor_exploration_locked = False

    def _has_mutation_capability(self) -> bool:
        return bool(self._available_tool_names & MUTATION_TOOLS)

    def _is_code_change_runtime(self) -> bool:
        policy = self.run_context.execution_policy
        if policy is None:
            return self.actor_id != "" and self._has_mutation_capability()
        return policy.strategy is not ExecutionStrategy.PLANNER_DIRECT

    def _exploration_loop_intervention(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        if (
            not self.actor_id
            or self._successful_mutations
            or not self._is_code_change_runtime()
            or not self._has_mutation_capability()
        ):
            return ""

        if self._actor_exploration_locked and tool_name not in MUTATION_TOOLS:
            return self._actor_mutation_required_message()

        is_exploration = tool_name in EXPLORATION_TOOLS
        if tool_name in {"bash", "run"}:
            is_exploration = self._is_source_reading_command(args.get("command"))
        if not is_exploration:
            return ""

        if not self._actor_exploration_locked:
            self._exploration_calls_without_mutation += 1
            if self._exploration_calls_without_mutation <= ACTOR_EXPLORATION_LIMIT:
                if self._exploration_calls_without_mutation == ACTOR_EXPLORATION_LIMIT:
                    self._actor_exploration_locked = True
                return ""
        return self._actor_mutation_required_message()

    @staticmethod
    def _actor_mutation_required_message() -> str:
        return (
            "System Alert: The source-exploration allowance for this code-change "
            "task is exhausted. Non-mutation tools are temporarily unavailable. "
            "Use the context already collected to make the smallest complete change with "
            "edit_file/write_file/apply_patch. After a successful edit, source "
            "inspection, tests, and diagnostics are available again."
        )

    @staticmethod
    def _is_source_reading_command(command: Any) -> bool:
        if not isinstance(command, str) or not command.strip():
            return False
        source_readers = re.compile(
            r"(?:^|[;&|]\s*)(?:"
            r"cat|head|tail|less|more|sed\s+-n|grep|rg|find|ls|dir|tree|"
            r"get-content|select-string|get-childitem"
            r")\b",
            flags=re.IGNORECASE,
        )
        return bool(source_readers.search(command.strip()))

    def _tool_schemas_for_step(
        self,
        tool_schemas: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed_names: set[str] | None = None
        if (
            self.actor_id
            and self._actor_exploration_locked
            and not self._successful_mutations
        ):
            allowed_names = MUTATION_TOOLS
        elif (
            not self.actor_id
            and self._planner_exploration_locked
            and not self._delegation_calls
        ):
            allowed_names = PLANNER_ORCHESTRATION_TOOLS
        if allowed_names is None:
            return tool_schemas
        return [
            schema
            for schema in tool_schemas
            if str(schema.get("function", {}).get("name") or schema.get("name") or "")
            in allowed_names
        ]

    def _planner_exploration_intervention(self, tool_name: str) -> str:
        if (
            not self.actor_id
            and self._planner_exploration_locked
            and not self._delegation_calls
            and tool_name not in PLANNER_ORCHESTRATION_TOOLS
        ):
            return self._planner_delegation_required_message()

        if (
            self.actor_id
            or self._delegation_calls
            or tool_name not in EXPLORATION_TOOLS
            or not self._is_code_change_runtime()
            or "delegate" not in self._available_tool_names
        ):
            return ""

        self._planner_exploration_calls += 1
        if self._planner_exploration_calls <= PLANNER_EXPLORATION_LIMIT:
            if self._planner_exploration_calls == PLANNER_EXPLORATION_LIMIT:
                self._planner_exploration_locked = True
            return ""
        return self._planner_delegation_required_message()

    @staticmethod
    def _planner_delegation_required_message() -> str:
        return (
            "System Alert: Planner has enough repository context for this code-change "
            "task. Only orchestration tools are temporarily available. Register one "
            "focused Coder task with update_state, then delegate it with the essential "
            "target files and concise findings."
        )

    def _outline_loop_intervention(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        """Redirect repeated outline reads toward actual source inspection."""
        raw_path = args.get("file_path") or args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return ""
        path_key = raw_path.replace("\\", "/").lower()
        source_tools = {
            "read",
            "read_file",
            "read_text_file",
            "edit",
            "edit_file",
            "write",
            "write_file",
        }
        if tool_name in source_tools:
            self._outline_reads_by_file.pop(path_key, None)
            return ""
        if tool_name != "read_outline":
            return ""

        previous_reads = self._outline_reads_by_file.get(path_key, 0)
        if previous_reads >= 2:
            offset = args.get("offset") or 1
            limit = args.get("limit") or 200
            return (
                "System Alert: read_outline only returns symbol signatures and this "
                f"file has already been outlined twice. Call read with file_path="
                f"'{raw_path}', offset={offset}, limit={limit} to inspect the actual "
                "source. Do not call read_outline again for implementation details."
            )
        self._outline_reads_by_file[path_key] = previous_reads + 1
        return ""

    @staticmethod
    def _decode_cached_tool_result(cached: str) -> tuple[ToolResult, str]:
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return ToolResult.ok(cached), cached
        if not isinstance(payload, dict):
            return ToolResult.ok(cached), cached
        observation = str(payload.get("observation", ""))
        success = bool(payload.get("success", True))
        if success:
            return ToolResult.ok(str(payload.get("content", observation))), observation
        return ToolResult.fail(
            str(payload.get("error", "cached tool call failed")),
            content=str(payload.get("content", "")),
        ), observation

    def _remember_tool_result(
        self,
        tool_call_id: str,
        result: ToolResult,
    ) -> None:
        if self.run_context.store is None or self.actor_id:
            return
        last_message = self.ctx.messages[-1] if self.ctx.messages else {}
        observation = str(last_message.get("content", ""))
        self.run_context.completed_tool_calls[tool_call_id] = json.dumps(
            {
                "success": result.success,
                "content": result.content,
                "error": result.error,
                "observation": observation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _persist_root(
        self,
        event_type: str,
        *,
        status: RunStatus | None = None,
        error: str = "",
    ) -> None:
        if self.actor_id:
            return
        await self.run_context.persist_checkpoint(
            self.ctx.messages,
            event_type=event_type,
            status=status,
            error=error,
        )

    def _pending_tool_calls(self) -> list[dict[str, Any]]:
        """Return tool calls missing observations at the last assistant boundary."""
        assistant_index: int | None = None
        tool_calls: list[dict[str, Any]] = []
        for index in range(len(self.ctx.messages) - 1, -1, -1):
            message = self.ctx.messages[index]
            if message.get("role") != "assistant":
                continue
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                assistant_index = index
                tool_calls = [
                    cast(dict[str, Any], call)
                    for call in raw_calls
                    if isinstance(call, dict)
                ]
            break
        if assistant_index is None:
            return []
        observed_ids = {
            str(message.get("tool_call_id", ""))
            for message in self.ctx.messages[assistant_index + 1:]
            if message.get("role") == "tool"
        }
        return [
            call for call in tool_calls
            if str(call.get("id", "")) not in observed_ids
        ]

    async def _process_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
    ) -> AsyncGenerator[AgentEvent, None]:
        for tc in tool_calls:
            parsed = parse_tool_call(tc)
            yield await self._emit(AgentEvent(
                type="tool_call",
                tool_name=parsed.tool_name,
                tool_args=parsed.args,
                actor_id=self.actor_id,
            ))
            policy_denial = self._planner_tool_policy_denial(parsed.tool_name)
            if policy_denial:
                tool_name = parsed.tool_name
                result = ToolResult.fail(policy_denial)
                self.ctx.add_tool_result(
                    str(tc.get("id", "")),
                    f"ERROR: {policy_denial}",
                )
                yield await self._emit(AgentEvent(
                    type="policy_denied",
                    content=policy_denial,
                    tool_name=tool_name,
                    actor_id=self.actor_id,
                ))
            else:
                tool_name, _, result, _ = await self._execute_single_tool(tc)
            self._remember_tool_result(str(tc.get("id", "")), result)
            await self._persist_root("tool_result")
            yield await self._emit(AgentEvent(
                type="tool_result",
                tool_name=tool_name,
                tool_result=result,
                actor_id=self.actor_id,
            ))
            if result.policy_denied:
                yield await self._emit(AgentEvent(
                    type="policy_denied",
                    content=result.error or "Tool action denied by execution policy",
                    tool_name=tool_name,
                    actor_id=self.actor_id,
                ))
            if not result.success and self.run_context.budget_ledger is not None:
                try:
                    await self.run_context.budget_ledger.charge_failed_tool_call()
                except BudgetExceeded as error:
                    self._terminal_budget_error = str(error)
                    self.last_result_success = False
                    self.ctx.add_assistant_message(content=str(error))
                    await self._persist_root(
                        "budget_exhausted",
                        status=RunStatus.FAILED,
                        error=str(error),
                    )
                    yield await self._emit(AgentEvent(
                        type="budget_exhausted",
                        content=str(error),
                        actor_id=self.actor_id,
                    ))
                    yield await self._emit(AgentEvent(
                        type="error",
                        content=str(error),
                        actor_id=self.actor_id,
                    ))
                    return
            for event in await self._run_after_tool_hook(tool_name, result):
                yield await self._emit(event)

    def _planner_tool_policy_denial(self, tool_name: str) -> str:
        policy = self.run_context.execution_policy
        if self.actor_id or policy is None:
            return ""
        if (
            policy.strategy is ExecutionStrategy.PLANNER_DIRECT
            and tool_name in {"apply_patch", "delegate"}
        ):
            return (
                f"{policy.strategy.value} strategy does not permit "
                f"Planner tool '{tool_name}'"
            )
        return ""

    async def _run_after_tool_hook(self, tool_name: str, result: ToolResult) -> list[AgentEvent]:
        if self.after_tool_call is None:
            return []
        return await self.after_tool_call(tool_name, result)

    async def _emit(self, event: AgentEvent) -> AgentEvent:
        event.actor_id = event.actor_id or self.actor_id
        event.task_id = event.task_id or self.actor_id
        await self.run_context.emit(event)
        return event

    async def _token_stats_event(self) -> AgentEvent:
        usage = await self.run_context.usage_snapshot()
        return await self._emit(AgentEvent(
            type="token_stats",
            content=json.dumps({
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "estimated": usage.estimated,
            }),
            actor_id=self.actor_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            usage_estimated=usage.estimated,
        ))

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
        *,
        resume: bool = False,
    ) -> str:
        final_content = ""
        async for event in self.run_stream(user_input, resume=resume):
            if event.type == "thought" and on_token is not None:
                on_token(event.token)
            elif event.type in {"done", "error"}:
                final_content = event.content
        return final_content

    async def run_stream(
        self,
        user_input: str,
        *,
        resume: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        if not resume:
            self.ctx.add_user_message(user_input)
        await self._persist_root("running", status=RunStatus.RUNNING)
        try:
            if resume:
                async for event in self._process_tool_calls(
                    self._pending_tool_calls()
                ):
                    yield event
            async for event in self._run_stream_loop():
                yield event
        except asyncio.CancelledError:
            await asyncio.shield(
                self._persist_root("paused", status=RunStatus.PAUSED)
            )
            raise
        except Exception as error:
            await asyncio.shield(
                self._persist_root(
                    "failed",
                    status=RunStatus.FAILED,
                    error=str(error),
                )
            )
            raise

    async def _run_stream_loop(self) -> AsyncGenerator[AgentEvent, None]:
        tool_schemas = await self._list_tool_schemas()

        ledger = self.run_context.budget_ledger
        if ledger is not None:
            try:
                await ledger.ensure_can_execute()
            except (BudgetExceeded, PolicyViolation) as error:
                event_type = (
                    "budget_exhausted"
                    if isinstance(error, BudgetExceeded)
                    else "policy_denied"
                )
                self.last_result_success = False
                self.ctx.add_assistant_message(content=str(error))
                await self._persist_root(
                    event_type,
                    status=RunStatus.FAILED,
                    error=str(error),
                )
                yield await self._emit(AgentEvent(
                    type=event_type,
                    content=str(error),
                    actor_id=self.actor_id,
                ))
                yield await self._emit(AgentEvent(
                    type="error",
                    content=str(error),
                    actor_id=self.actor_id,
                ))
                return

        step_count = 0
        while True:
            step_count += 1
            if step_count > self.max_steps:
                error_msg = "Safety stop: agent reached the maximum step limit."
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                await self._persist_root(
                    "failed",
                    status=RunStatus.FAILED,
                    error=error_msg,
                )
                if self.emit_token_stats:
                    yield await self._token_stats_event()
                yield await self._emit(AgentEvent(type="error", content=error_msg, actor_id=self.actor_id))
                return

            if self.ctx.needs_compression(self.llm):
                try:
                    if ledger is not None:
                        await ledger.claim_model_call()
                    compression_usage = await self.ctx.compress(self.llm)
                    await self.run_context.record_usage(
                        int(compression_usage.get("prompt_tokens", 0) or 0),
                        int(compression_usage.get("completion_tokens", 0) or 0),
                        bool(compression_usage.get("estimated", True)),
                    )
                except (BudgetExceeded, PolicyViolation) as error:
                    async for event in self._stop_for_policy_error(error):
                        yield event
                    return
                await self._persist_root("compaction")
                yield await self._emit(AgentEvent(type="compaction", actor_id=self.actor_id))
            elif self.ctx.needs_proactive_compression(self.llm):
                self.ctx._lightweight_compress()
                await self._persist_root("compaction")
                yield await self._emit(AgentEvent(type="compaction", content="lightweight", actor_id=self.actor_id))

            queue: asyncio.Queue[str] = asyncio.Queue()

            def on_token(token: str) -> None:
                queue.put_nowait(token)

            try:
                if ledger is not None:
                    await ledger.claim_model_call()
            except (BudgetExceeded, PolicyViolation) as error:
                async for event in self._stop_for_policy_error(error):
                    yield event
                return

            chat_task = asyncio.create_task(
                self.llm.chat(
                    messages=self._payload_messages(),
                    tools=(
                        self._tool_schemas_for_step(tool_schemas)
                        if tool_schemas
                        else None
                    ),
                    on_token=on_token,
                )
            )

            try:
                while not chat_task.done() or not queue.empty():
                    try:
                        token = await asyncio.wait_for(queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue
                    yield await self._emit(AgentEvent(
                        type="thought",
                        token=token,
                        content=token,
                        actor_id=self.actor_id,
                    ))
            finally:
                if not chat_task.done():
                    chat_task.cancel()

            try:
                response = await chat_task
                usage = response.get("_usage", {})
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                usage_estimated = bool(usage.get("estimated", True))
                await self.run_context.record_usage(
                    prompt_tokens,
                    completion_tokens,
                    usage_estimated,
                )
                yield await self._emit(AgentEvent(
                    type="model_usage",
                    content=json.dumps({
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "estimated": usage_estimated,
                    }),
                    actor_id=self.actor_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    usage_estimated=usage_estimated,
                ))
            except (BudgetExceeded, PolicyViolation) as error:
                async for event in self._stop_for_policy_error(error):
                    yield event
                return
            except LLMAPIError as e:
                error_msg = str(e)
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                await self._persist_root(
                    "failed",
                    status=RunStatus.FAILED,
                    error=error_msg,
                )
                if self.emit_token_stats:
                    yield await self._token_stats_event()
                yield await self._emit(AgentEvent(type="error", content=error_msg, actor_id=self.actor_id))
                return

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content") or ""
                if self._must_continue_for_missing_mutation(content):
                    continue
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                self.last_result_success = True
                await self._persist_root(
                    "completed",
                    status=RunStatus.COMPLETED,
                )
                if self.emit_token_stats:
                    yield await self._token_stats_event()
                yield await self._emit(AgentEvent(type="done", content=content, actor_id=self.actor_id))
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )
            await self._persist_root("assistant_tool_calls")

            async for event in self._process_tool_calls(tool_calls):
                yield event
            if self._terminal_budget_error:
                return

    def _must_continue_for_missing_mutation(self, content: str) -> bool:
        if (
            self._successful_mutations
            or not self._is_code_change_runtime()
            or not self._has_mutation_capability()
            or self._blocked_final_without_mutation >= 2
        ):
            return False
        self._blocked_final_without_mutation += 1
        self.ctx.add_system_message(
            "System Alert: This is a code-change execution path, but no successful "
            "edit/write/apply_patch action has occurred yet. Do not finish with a "
            "summary. Continue by producing the minimal required repository change, "
            "or, only if impossible, name the precise blocker after one final source "
            "inspection. Previous attempted final response was:\n"
            + content[:1200]
        )
        return True

    async def _stop_for_policy_error(
        self,
        error: BudgetExceeded | PolicyViolation,
    ) -> AsyncGenerator[AgentEvent, None]:
        event_type = (
            "budget_exhausted"
            if isinstance(error, BudgetExceeded)
            else "policy_denied"
        )
        error_msg = str(error)
        self.last_result_success = False
        self.ctx.add_assistant_message(content=error_msg)
        await self._persist_root(
            event_type,
            status=RunStatus.FAILED,
            error=error_msg,
        )
        if isinstance(error, BudgetExceeded):
            # record_usage updates the shared totals before the hard budget check
            # raises. Emit those totals so failed benchmark runs do not report
            # zero usage or omit the final over-budget model call.
            yield await self._token_stats_event()
        yield await self._emit(AgentEvent(
            type=event_type,
            content=error_msg,
            actor_id=self.actor_id,
        ))
        yield await self._emit(AgentEvent(
            type="error",
            content=error_msg,
            actor_id=self.actor_id,
        ))
