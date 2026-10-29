"""Lifecycle manager for generic Todo Workers.

Every Worker shares the parent session's durable Todo plan, claims one ready
task with a lease, and executes inside an isolated Git worktree when possible.
Conversation state remains private to the Worker; parent-child messages use the
durable mailbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from carbide.agents.domain import SubagentRequest, SubagentResult, SubagentStatus
from carbide.agents.worktree import WorktreeError, WorktreeHandle, WorktreeManager
from carbide.prompts import CODING_WORKFLOW
from carbide.providers.base import StreamChunk, StreamTextDelta
from carbide.todo.repository import TodoRepository
from carbide.todo.service import TodoService
from carbide.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from carbide.governance.session_store import SessionStore
    from carbide.sdk import Agent


WORKER_PROMPT = (
    """You are Carbide's generic Todo Worker.

Work on the shared parent-session Todo plan. First inspect the current plan and
call todo_claim to atomically claim one ready task. Do not choose a task merely
from the parent message and do not work on an unclaimed task. Complete the
claimed task with the available project tools, run focused checks when useful,
then call todo_update with the claimed item's expected plan version and
revision. If the task cannot be completed, report the blocker and mark it
failed or release it. After one task, return a complete evidence-backed result
to the parent; another Worker can claim the next task.

Use agent_message for material progress or focused questions. You cannot spawn
another Agent. The Todo plan is shared state, while your conversation and
workspace are isolated from the parent.
"""
    + "\n"
    + CODING_WORKFLOW
)

DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_HEARTBEAT_SECONDS = 10.0


class SubagentManager:
    """Own direct child lifecycles without sharing parent Agent state."""

    def __init__(
        self,
        parent: Agent,
        *,
        max_concurrency: int = 4,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and less than lease_seconds")
        self.parent = parent
        self.max_concurrency = max_concurrency
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._owner_id = str(uuid.uuid4())
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._tasks: dict[str, asyncio.Task[SubagentResult]] = {}
        self._background_runs: set[str] = set()
        self._completed: deque[SubagentResult] = deque()
        self._parent_waiters: dict[str, asyncio.Event] = {}
        self._worker_todo_services: dict[str, TodoService] = {}
        self.worktrees = WorktreeManager(parent.project_dir)
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closed = False
        if parent.session_id:
            self.store.reap_expired_agent_runs(
                parent_session_id=parent.session_id,
                exclude_owner_id=self._owner_id,
            )
            TodoService(
                TodoRepository(self.store),
                parent.session_id,
                actor_id=self._owner_id,
            ).reap_expired_claims()

    @property
    def store(self) -> SessionStore:
        store = self.parent.session_store
        if store is None:
            raise RuntimeError("SubAgents require session storage")
        return cast("SessionStore", store)

    async def dispatch(self, request: SubagentRequest) -> SubagentResult:
        """Start a child and optionally wait for its complete result."""
        if self._closed:
            raise RuntimeError("Subagent manager is closed")
        self._ensure_heartbeat()
        run_id = str(uuid.uuid4())
        task = asyncio.create_task(
            self._execute_with_limit(run_id, request),
            name=f"subagent:worker:{run_id[:8]}",
        )
        self._tasks[run_id] = task
        if request.background:
            self._background_runs.add(run_id)
        self.parent._track_task(task)
        task.add_done_callback(lambda done: self._on_done(run_id, done))

        if request.background:
            return self._running_result(run_id, request, "SubAgent started in background")

        timeout = request.timeout_seconds or 600.0
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            self._background_runs.add(run_id)
            return self._running_result(
                run_id,
                request,
                f"SubAgent exceeded {timeout:.0f}s and continues in background",
            )

    async def _execute_with_limit(
        self,
        run_id: str,
        request: SubagentRequest,
    ) -> SubagentResult:
        async with self._semaphore:
            return await self._execute(run_id, request)

    async def _execute(self, run_id: str, request: SubagentRequest) -> SubagentResult:
        child = None
        worktree: WorktreeHandle | None = None
        worker_todo = TodoService(
            TodoRepository(self.store),
            self.parent.session_id or "",
            actor_id=run_id,
        )
        self._worker_todo_services[run_id] = worker_todo
        metadata: dict[str, Any] = {}
        try:
            self.store.create_agent_run(
                run_id=run_id,
                parent_session_id=self.parent.session_id or "",
                child_session_id=None,
                mode="worker",
                role=None,
                task=request.task,
                background=request.background,
                owner_id=self._owner_id,
                lease_seconds=self.lease_seconds,
            )

            child_workspace = self.parent.project_dir
            try:
                worktree = await self.worktrees.prepare(run_id)
                child_workspace = worktree.workspace
                metadata["worktree_isolated"] = True
            except WorktreeError as exc:
                raise WorktreeError(
                    f"Generic Worker requires a Git repository for an isolated workspace: {exc}"
                ) from exc
            child_registry = self._child_registry(
                run_id,
                todo_service=worker_todo,
            )

            async def parent_messages() -> list[dict[str, Any]]:
                return self.store.claim_agent_messages(
                    run_id=run_id,
                    direction="parent_to_child",
                )

            from carbide.sdk import Agent

            child = Agent(
                provider=self.parent.provider,
                project_dir=child_workspace,
                system_prompt=WORKER_PROMPT,
                enable_memory=False,
                enable_skills=False,
                enable_rules=False,
                enable_compression=False,
                enable_governance=True,
                enable_session_store=True,
                enable_subagents=False,
                enable_todo_tools=True,
                close_provider=False,
                tool_registry=child_registry,
                max_turns=40,
                parent_message_provider=parent_messages,
                approval_callback=self.parent.loop.approval_callback,
                session_store_override=self.store,
                todo_service_override=worker_todo,
                todo_context_session_id=self.parent.session_id,
                todo_owner_id=run_id,
            )

            self.store.update_agent_run(
                run_id,
                status=SubagentStatus.RUNNING.value,
                owner_id=self._owner_id,
                lease_seconds=self.lease_seconds,
                child_session_id=child.session_id,
            )
            self._parent_waiters[run_id] = asyncio.Event()

            text_parts: list[str] = []
            usage: dict[str, int] = {}
            failure: str | None = None
            reason = "completed"

            async with child:
                async for event in child.query(
                    f"{request.task}\n\n{WORKER_PROMPT}",
                    tools=child_registry.api_schemas(),
                ):
                    if event.type == "chunk":
                        data = event.data
                        content = getattr(data, "content", None)
                        if isinstance(content, str) and content:
                            text_parts.append(content)
                        elif isinstance(data, StreamTextDelta):
                            text_parts.append(data.text)
                        elif isinstance(data, StreamChunk) and data.content:
                            text_parts.append(data.content)
                    elif event.type == "turn_complete" and isinstance(event.data, dict):
                        reason = str(event.data.get("reason", reason))
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "cache_read_tokens",
                            "cache_creation_tokens",
                        ):
                            usage[key] = usage.get(key, 0) + int(event.data.get(key, 0) or 0)
                    elif event.type == "error" and isinstance(event.data, dict):
                        failure = str(event.data.get("message", "subagent error"))

            final_text = "".join(text_parts)
            if failure is None and reason == "completed":
                status = SubagentStatus.COMPLETED
            else:
                status = SubagentStatus.FAILED
                failure = failure or f"SubAgent ended with reason: {reason}"

            if worktree is not None:
                if status is SubagentStatus.COMPLETED:
                    outcome = await self.worktrees.finalize(
                        worktree,
                        commit_message=f"carbide: implement subagent {run_id[:8]}",
                    )
                    metadata = {
                        **metadata,
                        "worktree_status": outcome.status,
                        **outcome.metadata,
                    }
                    if outcome.commit is not None:
                        metadata["commit"] = outcome.commit
                    if outcome.worktree_path is not None:
                        metadata["worktree_path"] = outcome.worktree_path
                    if outcome.status == "merge_blocked":
                        status = SubagentStatus.MERGE_BLOCKED
                        failure = outcome.error or "Automatic merge was blocked"
                    elif outcome.status == "failed":
                        status = SubagentStatus.FAILED
                        failure = outcome.error or "Worktree finalization failed"
                        await self.worktrees.cleanup(worktree)
                        worktree = None
                    else:
                        worktree = None
                else:
                    await self.worktrees.cleanup(worktree)
                    worktree = None

            if worker_todo is not None:
                worker_todo.release_owner(
                    run_id,
                    requeue=status is not SubagentStatus.COMPLETED,
                )
                self._worker_todo_services.pop(run_id, None)

            result = SubagentResult(
                agent_id=run_id,
                parent_session_id=self.parent.session_id or "",
                child_session_id=child.session_id,
                status=status,
                final_text=final_text,
                summary=_summary(final_text),
                error=failure,
                usage=usage,
                metadata=metadata,
            )
            self.store.update_agent_run(
                run_id,
                status=status.value,
                owner_id=self._owner_id,
                result={"final_text": final_text, "metadata": metadata},
                summary=result.summary,
                error=failure,
                usage=usage,
            )
            self._publish_background_result(request, result)
            return result
        except asyncio.CancelledError:
            if worktree is not None:
                with contextlib.suppress(Exception):
                    await self.worktrees.cleanup(worktree)
            with contextlib.suppress(Exception):
                if worker_todo is not None:
                    worker_todo.release_owner(run_id, requeue=True)
                    self._worker_todo_services.pop(run_id, None)
                self.store.update_agent_run(
                    run_id,
                    status=SubagentStatus.CANCELLED.value,
                    owner_id=self._owner_id,
                    error="SubAgent cancelled",
                )
                self._publish_background_result(
                    request,
                    SubagentResult(
                        agent_id=run_id,
                        parent_session_id=self.parent.session_id or "",
                        status=SubagentStatus.CANCELLED,
                        error="SubAgent cancelled",
                        summary="SubAgent cancelled",
                    ),
                )
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if worktree is not None:
                with contextlib.suppress(Exception):
                    await self.worktrees.cleanup(worktree)
            with contextlib.suppress(Exception):
                if worker_todo is not None:
                    worker_todo.release_owner(run_id, requeue=True)
                    self._worker_todo_services.pop(run_id, None)
                self.store.update_agent_run(
                    run_id,
                    status=SubagentStatus.FAILED.value,
                    owner_id=self._owner_id,
                    error=error,
                )
            result = SubagentResult(
                agent_id=run_id,
                parent_session_id=self.parent.session_id or "",
                child_session_id=getattr(child, "session_id", None),
                status=SubagentStatus.FAILED,
                error=error,
                summary=error,
            )
            self._publish_background_result(request, result)
            return result
        finally:
            self._parent_waiters.pop(run_id, None)

    def _publish_background_result(
        self,
        request: SubagentRequest,
        result: SubagentResult,
    ) -> None:
        """Publish a complete terminal result to a background parent mailbox."""
        if not request.background:
            return
        with contextlib.suppress(Exception):
            if self.store.get_agent_run(result.agent_id) is None:
                return
            self.store.append_agent_message(
                run_id=result.agent_id,
                direction="child_to_parent",
                kind="result",
                content=result.payload(),
                owner_id=self._owner_id,
            )

    def _child_registry(
        self,
        run_id: str,
        *,
        todo_service: TodoService,
    ) -> ToolRegistry:
        """Build the single generic Worker capability registry."""
        registry = ToolRegistry()
        parent_registry = self.parent.loop.tool_registry
        if parent_registry is None:
            raise RuntimeError("SubAgents require a parent tool registry")
        control_tools = {
            "agent",
            "agent_status",
            "agent_send",
            "agent_cancel",
            "agent_message",
            "load_skill",
            "snip",  # Bound to the parent's context; never share its deletion queue.
        }
        for tool in parent_registry.all():
            if tool.name in control_tools or tool.name.startswith("todo_"):
                continue
            registry.register(tool)
        from carbide.tools.todo import register_todo_tools

        register_todo_tools(registry, todo_service)
        # Import lazily: the model-facing tools import this manager for their
        # type, while this method is only called after both modules are loaded.
        from carbide.agents.tools import AgentMessageTool

        registry.register(AgentMessageTool(self, run_id))
        return registry

    def child_message(
        self,
        run_id: str,
        *,
        kind: str,
        content: Any,
        wait_for_answer: bool = False,
        timeout_seconds: float = 300.0,
    ) -> str:
        """Queue a child-to-parent message and optionally wait asynchronously.

        The actual wait is performed by :meth:`wait_for_parent` because tool
        implementations are async while this storage boundary is deliberately
        synchronous.  Keeping the message durable before waiting means a
        parent restart can still observe the question.
        """
        record = self.store.get_agent_run(run_id)
        if record is None:
            raise ValueError(f"Unknown SubAgent: {run_id}")
        if record["status"] not in {
            SubagentStatus.QUEUED.value,
            SubagentStatus.RUNNING.value,
            SubagentStatus.WAITING_FOR_PARENT.value,
        }:
            raise ValueError(f"SubAgent is no longer running: {record['status']}")
        if not kind.strip():
            raise ValueError("message kind must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        message_id = self.store.append_agent_message(
            run_id=run_id,
            direction="child_to_parent",
            kind=kind,
            content=content,
            owner_id=self._owner_id,
        )
        if wait_for_answer and record["background"]:
            self.store.update_agent_run(
                run_id,
                status=SubagentStatus.WAITING_FOR_PARENT.value,
                owner_id=self._owner_id,
                lease_seconds=self.lease_seconds,
            )
        return message_id

    async def wait_for_parent(self, run_id: str, *, timeout_seconds: float) -> bool:
        """Wait for a parent message after a child asked a question."""
        waiter = self._parent_waiters.get(run_id)
        if waiter is None:
            raise ValueError(f"SubAgent is not accepting parent messages: {run_id}")
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_seconds)
        except TimeoutError:
            self.store.update_agent_run(
                run_id,
                status=SubagentStatus.RUNNING.value,
                owner_id=self._owner_id,
                lease_seconds=self.lease_seconds,
            )
            return False
        self.store.update_agent_run(
            run_id,
            status=SubagentStatus.RUNNING.value,
            owner_id=self._owner_id,
            lease_seconds=self.lease_seconds,
        )
        waiter.clear()
        return True

    def parent_messages(self) -> list[dict[str, Any]]:
        """Claim child messages for injection into the parent model context."""
        return self.store.claim_agent_messages_for_parent(
            parent_session_id=self.parent.session_id or "",
            direction="child_to_parent",
        )

    def _on_done(self, run_id: str, task: asyncio.Task[SubagentResult]) -> None:
        self._tasks.pop(run_id, None)
        if run_id not in self._background_runs:
            return
        self._background_runs.discard(run_id)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            self._completed.append(task.result())

    def _running_result(
        self,
        run_id: str,
        request: SubagentRequest,
        summary: str,
    ) -> SubagentResult:
        return SubagentResult(
            agent_id=run_id,
            parent_session_id=self.parent.session_id or "",
            status=SubagentStatus.RUNNING,
            summary=summary,
        )

    def status(self, agent_id: str) -> dict[str, Any] | None:
        return self.store.get_agent_run(agent_id)

    def list_runs(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_agent_runs(self.parent.session_id or "", status=status)

    def send(self, agent_id: str, *, kind: str, content: Any) -> str:
        record = self.store.get_agent_run(agent_id)
        if record is None:
            raise ValueError(f"Unknown SubAgent: {agent_id}")
        if record["status"] not in {
            SubagentStatus.QUEUED.value,
            SubagentStatus.RUNNING.value,
            SubagentStatus.WAITING_FOR_PARENT.value,
        }:
            raise ValueError(f"SubAgent is no longer running: {record['status']}")
        message_id = self.store.append_agent_message(
            run_id=agent_id,
            direction="parent_to_child",
            kind=kind,
            content=content,
        )
        waiter = self._parent_waiters.get(agent_id)
        if waiter is not None and record["status"] == SubagentStatus.WAITING_FOR_PARENT.value:
            waiter.set()
        return message_id

    async def cancel(self, agent_id: str) -> bool:
        task = self._tasks.get(agent_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def poll_completed(self) -> list[dict[str, Any]]:
        """Drain completed background results for CLI/SDK surfaces."""
        return [result.payload() for result in _drain(self._completed)]

    def stats(self) -> dict[str, int]:
        return {
            "running": len(self._tasks),
            "background": len(self._background_runs),
            "completed_pending": len(self._completed),
            "max_concurrency": self.max_concurrency,
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ensure_heartbeat(self) -> None:
        """Start one manager heartbeat task lazily on the first child."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"subagent-heartbeat:{self._owner_id[:8]}",
            )

    async def _heartbeat_loop(self) -> None:
        """Renew active leases until the manager is closed."""
        try:
            while not self._closed:
                await asyncio.sleep(self.heartbeat_seconds)
                if self._closed or not self.parent.session_id:
                    continue
                with contextlib.suppress(Exception):
                    self.store.heartbeat_agent_runs(
                        parent_session_id=self.parent.session_id,
                        owner_id=self._owner_id,
                        lease_seconds=self.lease_seconds,
                    )
                    coordinator = TodoService(
                        TodoRepository(self.store),
                        self.parent.session_id,
                        actor_id=self._owner_id,
                    )
                    coordinator.reap_expired_claims()
                    for run_id in tuple(self._worker_todo_services):
                        coordinator.renew_owner(
                            run_id,
                            lease_seconds=self.lease_seconds,
                        )
        except asyncio.CancelledError:
            raise


def _summary(text: str) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= 240 else compact[:239].rstrip() + "…"


def _drain(queue: deque[SubagentResult]) -> list[SubagentResult]:
    values: list[SubagentResult] = []
    while queue:
        values.append(queue.popleft())
    return values
