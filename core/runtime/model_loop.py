from __future__ import annotations

import json
import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast

from .conversation import ContextManager
from .loop_control import RuntimeLoopControl
from .tool_calls import WORKSPACE_AWARE_TOOLS, parse_tool_call
from .message_payload import build_message_payload
from ..application.run_lifecycle import RunLifecycleService
from ..events import AgentEvent
from ..exceptions import LLMAPIError
from ..execution.policy import BudgetExceeded, PolicyViolation
from ..execution.models import ExecutionStrategy
from ..llm import LLMClient
from ..runs.context import RunContext
from ..tools.base import BaseTool, ToolResult
from ..tools.catalog import ToolCatalog
from ..tools.gateway import ToolGateway, provider_registration
from ..tools.models import ToolCall
from ..security.manager import SecurityManager
from ..security.models import GuardOutcome, GuardStage, SecurityOutcome
from ..security.redaction import redact_structure


class ModelLoop:
    """Model/tool iteration only; application orchestration lives elsewhere."""

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
        manage_run_lifecycle: bool = True,
        security_manager: SecurityManager | None = None,
        role: str = "planner",
        lifecycle_service: RunLifecycleService | None = None,
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
        self.manage_run_lifecycle = manage_run_lifecycle
        self.run_context = run_context or RunContext.create()
        self.security_manager = security_manager
        self.role = role
        self.lifecycle_service = lifecycle_service or RunLifecycleService(
            self.run_context,
            self.ctx.messages,
            enabled=manage_run_lifecycle,
        )
        self.tools_by_name = {t.name: t for t in tools} if tools else {}
        self.tool_catalog = ToolCatalog()
        for tool in self.tools_by_name.values():
            self.tool_catalog.register_local(
                tool,
                workspace_aware=tool.name in WORKSPACE_AWARE_TOOLS,
            )
        self.tool_gateway = ToolGateway(
            self.tool_catalog,
            workspace_dir=self.workspace_dir,
            security_manager=self.security_manager,
        )
        self._loop_control = RuntimeLoopControl(actor_id)
        self.last_result_success = True
        self._terminal_budget_error = ""

    async def _list_tool_schemas(self) -> list[dict[str, Any]]:
        if self.tool_provider is not None:
            schemas = cast(list[dict[str, Any]], await self.tool_provider.list_tools())
            for schema in schemas:
                function = schema.get("function", {})
                name = str(
                    function.get("name", "")
                    if isinstance(function, dict)
                    else schema.get("name", "")
                )
                if name:
                    self.tool_catalog.register(provider_registration(
                        name=name,
                        schema=schema,
                        provider=self.tool_provider,
                        workspace_aware=name in WORKSPACE_AWARE_TOOLS,
                    ))
        else:
            schemas = [t.schema for t in self.tools_by_name.values()]
        available_tool_names = {
            str(schema.get("function", {}).get("name") or schema.get("name") or "")
            for schema in schemas
            if isinstance(schema, dict)
        }
        available_tool_names.discard("")
        self._loop_control.set_available_tools(available_tool_names)
        return schemas

    def _payload_messages(self) -> list[dict[str, Any]]:
        return build_message_payload(
            self.ctx.messages,
            self.dynamic_context_builder,
        )

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

        planner_intervention = self._loop_control.planner_intervention(
            tool_name,
            self.run_context.execution_policy,
        )
        if planner_intervention:
            result = ToolResult.ok(planner_intervention)
            self.ctx.add_tool_result(tool_call_id, planner_intervention)
            return tool_name, args, result, True

        exploration_intervention = self._loop_control.actor_intervention(
            tool_name,
            args,
            self.run_context.execution_policy,
        )
        if exploration_intervention:
            result = ToolResult.ok(exploration_intervention)
            self.ctx.add_tool_result(tool_call_id, exploration_intervention)
            return tool_name, args, result, True

        outline_intervention = self._loop_control.outline_intervention(
            tool_name,
            args,
        )
        if outline_intervention:
            result = ToolResult.ok(outline_intervention)
            self.ctx.add_tool_result(tool_call_id, outline_intervention)
            return tool_name, args, result, True

        if self._loop_control.repeated_action(tool_name, args):
            intervention = "System Alert: Repeated tool call detected. Please try a different approach."
            result = ToolResult.fail(intervention)
            self.ctx.add_tool_result(tc["id"], intervention)
            return tool_name, args, result, True

        result = await self.tool_gateway.execute(ToolCall(
            call_id=tool_call_id,
            name=tool_name,
            arguments=args,
            run_id=self.run_context.run_id,
            actor_id=self.actor_id,
            role=self.role,
            workspace_identity=self.workspace_dir,
            correlation_id=tool_call_id,
        ))

        if result.success:
            observation = result.content
        else:
            observation = result.content or f"ERROR: {result.error}"

        self._loop_control.record_tool_result(tool_name, result)
        self.ctx.add_tool_result(tc["id"], observation)
        return tool_name, args, result, False

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
        status: str | None = None,
        error: str = "",
    ) -> None:
        if self.actor_id:
            return
        await self.lifecycle_service.persist(
            event_type=event_type,
            terminal_status=status,
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
            sanitized_args = redact_structure(parsed.args).value
            yield await self._emit(AgentEvent(
                type="tool_call",
                tool_name=parsed.tool_name,
                tool_args=(
                    cast(dict[str, Any], sanitized_args)
                    if isinstance(sanitized_args, dict)
                    else {}
                ),
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
                        status="failed",
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
        await self._persist_root("running", status="running")
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
                self._persist_root("paused", status="paused")
            )
            raise
        except Exception as error:
            await asyncio.shield(
                self._persist_root(
                    "failed",
                    status="failed",
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
                    status="failed",
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
                    status="failed",
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

            if self.security_manager is not None:
                last_message = self._payload_messages()[-1] if self._payload_messages() else {}
                pre_model_text = str(last_message.get("content", ""))
                pre_model = await self.security_manager.inspect(
                    stage=GuardStage.PRE_MODEL,
                    text=pre_model_text,
                    actor_id=self.actor_id,
                    role=self.role,
                    data_classification="user_content",
                )
                if pre_model.outcome is GuardOutcome.DENY:
                    async for event in self._stop_for_security_denial(
                        "Model call blocked by security policy."
                    ):
                        yield event
                    return

            chat_task = asyncio.create_task(
                self.llm.chat(
                    messages=self._payload_messages(),
                    tools=(
                        self._loop_control.schemas_for_step(tool_schemas)
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
                    status="failed",
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
                if self.security_manager is not None:
                    content = await self.security_manager.redact_output(
                        content,
                        stage=GuardStage.FINAL_OUTPUT,
                        actor_id=self.actor_id,
                    )
                    final_guard = await self.security_manager.inspect(
                        stage=GuardStage.FINAL_OUTPUT,
                        text=content,
                        actor_id=self.actor_id,
                        role=self.role,
                    )
                    if final_guard.outcome is GuardOutcome.DENY:
                        content = (
                            "The generated response was withheld by the output "
                            "security policy. No blocked content was persisted."
                        )
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                self.last_result_success = True
                await self._persist_root(
                    "completed",
                    status="completed",
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
        if not self._loop_control.should_continue_for_missing_mutation(
            self.run_context.execution_policy
        ):
            return False
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
            status="failed",
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

    async def _stop_for_security_denial(
        self,
        error_msg: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        self.last_result_success = False
        self.ctx.add_assistant_message(content=error_msg)
        await self._persist_root(
            "security_denied",
            status="failed",
            error=error_msg,
        )
        yield await self._emit(AgentEvent(
            type="security_decision",
            content=error_msg,
            actor_id=self.actor_id,
        ))
        yield await self._emit(AgentEvent(
            type="error",
            content=error_msg,
            actor_id=self.actor_id,
        ))
