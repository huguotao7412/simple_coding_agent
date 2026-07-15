from __future__ import annotations

import asyncio
import json

from cli.report import RunReport
from cli.ui import UI
from core.planner import Planner


class Bridge:
    """Connects the core Planner to the terminal UI. Runs the REPL loop."""

    def __init__(self, agent: Planner, ui: UI):
        self.agent = agent
        self.ui = ui

    async def run(self) -> None:
        self.ui.render_welcome()
        while True:
            try:
                user_input = await asyncio.to_thread(self.ui.render_user_prompt)
            except (EOFError, KeyboardInterrupt):
                self.ui.render_info("\nGoodbye.")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                self.ui.render_info("Goodbye.")
                break

            stream = None
            streamed_anything = False
            report = RunReport()
            try:
                async for event in self.agent.run_stream(user_input):
                    report.observe(event)

                    if event.type == "task_assessment":
                        try:
                            assessment = json.loads(event.content)
                            self.ui.render_info(
                                "Task assessment: "
                                f"strategy={assessment.get('strategy', 'unknown')}, "
                                f"complexity={assessment.get('complexity', 'unknown')}, "
                                f"risk={assessment.get('risk', 'unknown')}"
                            )
                        except (json.JSONDecodeError, AttributeError):
                            pass

                    elif event.type == "execution_policy":
                        try:
                            policy = json.loads(event.content)
                            budget = policy.get("budget", {})
                            self.ui.render_info(
                                "Execution policy: "
                                f"actors={policy.get('max_actors', 'unknown')}, "
                                f"model_calls={budget.get('max_model_calls', 'unknown')}, "
                                f"tokens={budget.get('max_total_tokens', 'unknown')}"
                            )
                        except (json.JSONDecodeError, AttributeError):
                            pass

                    elif event.type == "thought":
                        self.ui.clear_tool_status()
                        if stream is None:
                            stream = self.ui.stream_markdown()
                            stream.__enter__()
                        stream.add_token(event.token)
                        streamed_anything = True

                    elif event.type == "tool_call":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        self.ui.render_tool_call(event.tool_name or "tool", event.tool_args)
                        self.ui.render_tool_status(event.tool_name or "tool", "running")

                    elif event.type == "tool_result":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        self.ui.clear_tool_status()
                        success = bool(event.tool_result and event.tool_result.success)
                        detail = ""
                        if event.tool_result:
                            detail = event.tool_result.content if success else (event.tool_result.error or "")
                        self.ui.render_tool_result(event.tool_name or "tool", success=success, detail=detail)

                    elif event.type == "actor_update":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        try:
                            snapshot = json.loads(event.content)
                            self.ui.render_actor_status(snapshot.get("task_tree", {}))
                        except Exception:
                            pass

                    elif event.type == "token_stats":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        try:
                            stats = json.loads(event.content)
                            self.ui.render_token_stats(
                                prompt_tokens=stats.get("prompt_tokens", 0),
                                completion_tokens=stats.get("completion_tokens", 0),
                            )
                        except Exception:
                            pass

                    elif event.type == "compaction":
                        self.ui.clear_tool_status()
                        self.ui.render_compaction(event.content)

                    elif event.type == "error":
                        self.ui.clear_tool_status()
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        self.ui.render_error(f"Agent Error: {event.content}")

                    elif event.type == "done":
                        self.ui.clear_tool_status()
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        if event.content and not streamed_anything:
                            self.ui.render_markdown(event.content)
            finally:
                self.ui.clear_tool_status()
                self.ui.clear_actor_status()
                if stream:
                    stream.__exit__(None, None, None)
                self.ui.render_run_report(report)
                try:
                    report_path = report.write_final_report(
                        self.agent.workspace_dir,
                        self.agent.run_context.run_id,
                    )
                except OSError as exc:
                    self.ui.render_info(f"Warning: final report could not be written: {exc}")
                else:
                    self.ui.render_info(f"Final report written to {report_path}")
