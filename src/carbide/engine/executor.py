"""Streaming tool executor with integrated governance.

Enhanced executor that includes permission checks, hooks, bash guard, and audit logging.
This is the unified executor that combines streaming dispatch with governance.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from carbide.providers.base import StreamToolUse

if TYPE_CHECKING:
    from carbide.governance import AuditLogger, HookRegistry, PermissionEngine
    from carbide.governance.bashguard import BashGuard
    from carbide.tools.base import ToolContext, ToolResult
    from carbide.tools.registry import ToolRegistry


@dataclass(slots=True)
class CompletedTool:
    call: StreamToolUse
    result: ToolResult
    duration_ms: float


ApprovalCallback = Callable[[StreamToolUse, str], Awaitable[bool] | bool]


class StreamingToolExecutor:
    """Dispatches tool calls with governance integration.

    Features:
    - Concurrent tool execution (when safe)
    - Permission checking
    - Bash command analysis
    - Pre/post hooks
    - Audit logging
    """

    def __init__(
        self,
        registry: ToolRegistry,
        ctx: ToolContext,
        permission_engine: PermissionEngine | None = None,
        hook_registry: HookRegistry | None = None,
        audit_logger: AuditLogger | None = None,
        bash_guard: BashGuard | None = None,
        approval_callback: ApprovalCallback | None = None,
        task_tracker: Callable[[asyncio.Task[Any]], None] | None = None,
        metrics_collector: Any | None = None,
    ) -> None:
        self._registry = registry
        self._ctx = ctx
        self._permission = permission_engine
        self._hooks = hook_registry
        self._audit = audit_logger
        self._bash_guard = bash_guard
        self._approval_callback = approval_callback
        self._task_tracker = task_tracker
        self._metrics = metrics_collector
        self._metrics_recorded: set[str] = set()

        self._order: list[StreamToolUse] = []
        self._tasks: dict[str, asyncio.Task[CompletedTool]] = {}
        self._barrier: list[asyncio.Task[CompletedTool]] = []

    @property
    def dispatched(self) -> int:
        return len(self._order)

    def add_tool(self, call: StreamToolUse) -> None:
        """Start a tool in the background with governance checks."""
        self._order.append(call)
        safe = self._registry.is_concurrency_safe(call.name)
        waits = list(self._tasks.values()) if not safe else list(self._barrier)
        task = asyncio.create_task(self._run(call, waits), name=f"tool:{call.name}:{call.id}")
        if self._task_tracker is not None:
            self._task_tracker(task)
        self._tasks[call.id] = task
        if not safe:
            self._barrier = [task]

    async def _run(
        self, call: StreamToolUse, waits: list[asyncio.Task[CompletedTool]]
    ) -> CompletedTool:
        """Execute a single tool with full governance workflow."""
        if waits:
            await asyncio.wait(waits)

        started = time.perf_counter()

        try:
            # Get the tool
            tool = self._registry.get(call.name)
            if tool is None:
                return CompletedTool(
                    call=call,
                    result=self._make_error_result(f"Unknown tool: {call.name}"),
                    duration_ms=(time.perf_counter() - started) * 1000,
                )

            # 1. Permission check
            args = tool.params(**call.arguments)
            if self._permission:
                permission_result = self._permission.check(tool, args)

                # Audit permission check
                if self._audit:
                    self._audit.log_permission_check(
                        tool_name=call.name,
                        args=call.arguments,
                        decision="approved" if permission_result.allowed else "denied",
                    )

                if not permission_result.allowed:
                    # PROMPT mode returns a question. If the caller provides
                    # an approval callback, ask it here; otherwise return the
                    # exact policy explanation instead of crashing on a
                    # missing ``PermissionResult.reason`` attribute.
                    prompt = permission_result.prompt
                    if permission_result.requires_approval and self._approval_callback and prompt:
                        try:
                            decision = self._approval_callback(call, prompt)
                            approved = (
                                await decision if inspect.isawaitable(decision) else bool(decision)
                            )
                        except Exception:
                            approved = False
                        if not approved:
                            reason = "Permission denied by user"
                            return CompletedTool(
                                call=call,
                                result=self._make_error_result(reason),
                                duration_ms=(time.perf_counter() - started) * 1000,
                            )
                    else:
                        reason = (
                            permission_result.reason
                            or permission_result.prompt
                            or "Permission denied by policy"
                        )
                        return CompletedTool(
                            call=call,
                            result=self._make_error_result(reason),
                            duration_ms=(time.perf_counter() - started) * 1000,
                        )

            # 2. Bash Guard check (for bash/shell tools)
            if call.name.lower() in ("bash", "shell") and self._bash_guard:
                command = call.arguments.get("command", "")
                if command:
                    is_safe = self._bash_guard.is_safe(command)

                    if not is_safe:
                        findings = self._bash_guard.analyze(command)
                        issues = [f.message for f in findings]

                        if self._audit:
                            # log_error is the closest available method
                            self._audit.log_error(
                                call.name, f"Command blocked: {', '.join(issues)}"
                            )

                        return CompletedTool(
                            call=call,
                            result=self._make_error_result(
                                f"Command blocked by BashGuard: {', '.join(issues)}"
                            ),
                            duration_ms=(time.perf_counter() - started) * 1000,
                        )

            # 3. PRE_TOOL Hook
            if self._hooks:
                try:
                    # HookRegistry methods are synchronous
                    from carbide.governance.hooks import HookContext

                    hook_context = HookContext(
                        tool_name=call.name,
                        arguments=args,
                        workspace=str(self._ctx.workspace),
                    )
                    hook_result = self._hooks.run_before(tool, hook_context)
                    if hook_result is not None:
                        # Hook short-circuited execution
                        return CompletedTool(
                            call=call,
                            result=hook_result,
                            duration_ms=(time.perf_counter() - started) * 1000,
                        )
                except Exception:
                    # Hook failure should not block execution
                    pass

            # 4. Execute the tool
            result = await self._registry.invoke(call.name, call.arguments, self._ctx)

            # 5. POST_TOOL Hook
            if self._hooks:
                try:
                    # HookRegistry methods are synchronous
                    from carbide.governance.hooks import HookContext

                    hook_context = HookContext(
                        tool_name=call.name,
                        arguments=args,
                        workspace=str(self._ctx.workspace),
                    )
                    modified_result = self._hooks.run_after(tool, hook_context, result)
                    if modified_result is not None:
                        result = modified_result
                except Exception:
                    # Hook failure should not affect the result
                    pass

            # 6. Audit logging
            if self._audit:
                duration_ms = (time.perf_counter() - started) * 1000
                self._audit.log_tool_result(
                    tool_name=call.name,
                    result=result.content if hasattr(result, "content") else str(result),
                    is_error=result.is_error if hasattr(result, "is_error") else False,
                    duration_ms=duration_ms,
                )

            return CompletedTool(
                call=call, result=result, duration_ms=(time.perf_counter() - started) * 1000
            )

        except Exception as e:
            # Audit errors
            if self._audit:
                self._audit.log_error(call.name, str(e))

            return CompletedTool(
                call=call,
                result=self._make_error_result(f"{type(e).__name__}: {e}"),
                duration_ms=(time.perf_counter() - started) * 1000,
            )

    def _make_error_result(self, error_message: str) -> ToolResult:
        """Create an error result."""
        from carbide.tools.base import ToolResult

        return ToolResult(content=error_message, is_error=True)

    async def gather_remaining(self) -> list[CompletedTool]:
        """Collect every dispatched tool, in dispatch order."""
        if not self._tasks:
            return []
        await asyncio.wait(self._tasks.values())
        completed: list[CompletedTool] = []
        for call in self._order:
            task = self._tasks[call.id]
            completed.append(self._harvest(call, task))
        self._record_metrics(completed)
        return completed

    def _record_metrics(self, completed: list[CompletedTool]) -> None:
        """Record harvested results without letting observability affect execution."""
        if self._metrics is None:
            return
        from datetime import UTC, datetime

        from carbide.execution.metrics import ExecutionMetrics

        for item in completed:
            if item.call.id in self._metrics_recorded:
                continue
            try:
                self._metrics.record(
                    ExecutionMetrics(
                        tool_name=item.call.name,
                        duration_ms=item.duration_ms,
                        timestamp=datetime.now(UTC),
                        success=not item.result.is_error,
                        error_type=(
                            item.result.content.split(":", 1)[0]
                            if item.result.is_error and ":" in item.result.content
                            else None
                        ),
                    )
                )
                self._metrics_recorded.add(item.call.id)
            except Exception:
                # Metrics are diagnostic only; a custom collector must never
                # turn a successful tool execution into a failed one.
                continue

    def _harvest(self, call: StreamToolUse, task: asyncio.Task[CompletedTool]) -> CompletedTool:
        """Extract result from a completed task."""
        from carbide.tools.base import ToolResult

        if task.cancelled():
            return CompletedTool(
                call=call,
                result=ToolResult(content="Tool execution was cancelled.", is_error=True),
                duration_ms=0.0,
            )
        if exc := task.exception():
            return CompletedTool(
                call=call,
                result=ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True),
                duration_ms=0.0,
            )
        return task.result()

    async def abort(self) -> list[CompletedTool]:
        """Cancel in-flight tools, still returning a result for each call."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        return await self.gather_remaining()

    def to_result_blocks(self, completed: list[CompletedTool]) -> list[dict[str, Any]]:
        """Render results as tool_result blocks for the next request."""
        return [
            {
                "type": "tool_result",
                "tool_use_id": item.call.id,
                "content": item.result.content,
                **({"is_error": True} if item.result.is_error else {}),
            }
            for item in completed
        ]
