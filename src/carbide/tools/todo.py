"""Thin model-tool adapters for the session Todo service."""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.todo.domain import TodoStatus
from carbide.todo.errors import TodoError
from carbide.todo.service import TodoService
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.registry import ToolRegistry


def _render_error(exc: TodoError) -> ToolResult:
    return ToolResult(content=f"[{exc.code}] {exc}", is_error=True)


def _render_snapshot(snapshot: object) -> str:
    # TodoService always returns TodoSnapshot. Keeping this adapter defensive
    # prevents a malformed custom service from crashing the tool executor.
    render = getattr(snapshot, "render", None)
    return str(render()) if callable(render) else str(snapshot)


class TodoTool:
    """Shared validation and service binding for the concrete tools."""

    def __init__(self, service: TodoService) -> None:
        self.service = service

    def _check_context(self, ctx: ToolContext) -> ToolResult | None:
        context_session_id = ctx.todo_session_id or ctx.session_id
        if (
            context_session_id is not None
            and context_session_id != self.service.session_id
        ):
            return ToolResult(content="[todo_context_error] session_id mismatch", is_error=True)
        return None


class TodoInitParams(BaseModel):
    pass


class TodoInitTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_init"
    description: ClassVar[str] = (
        "Initialize the session Todo plan. A session can have only one active plan; "
        "call this before adding tasks."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoInitParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del args
        if error := self._check_context(ctx):
            return error
        try:
            return ToolResult(content=_render_snapshot(self.service.initialize()))
        except TodoError as exc:
            return _render_error(exc)


class TodoListParams(BaseModel):
    max_chars: int = Field(default=8_000, ge=500, le=12_000)


class TodoListTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_list"
    description: ClassVar[str] = (
        "Read the current session Todo plan, including task status, IDs, versions, "
        "and dependencies. Derived ready/blocked status is computed, not stored."
    )
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = TodoListParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoListParams)
        if error := self._check_context(ctx):
            return error
        snapshot = self.service.snapshot()
        if snapshot is None:
            return ToolResult(content="No Todo plan initialized. Call todo_init first.")
        return ToolResult(content=snapshot.render(max_chars=args.max_chars))


class TodoClaimParams(BaseModel):
    preferred_item_id: str | None = Field(default=None, min_length=1)
    lease_seconds: float = Field(default=30.0, gt=0, le=3_600)


class TodoClaimTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_claim"
    description: ClassVar[str] = (
        "Atomically claim one dependency-ready Todo task for this Worker. "
        "The runtime supplies the owner identity; never invent one."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoClaimParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoClaimParams)
        if error := self._check_context(ctx):
            return error
        if not ctx.todo_owner_id:
            return ToolResult(
                content="[todo_claim_error] only a Worker can claim a Todo task",
                is_error=True,
            )
        try:
            claim = self.service.claim_next(
                ctx.todo_owner_id,
                lease_seconds=args.lease_seconds,
                preferred_item_id=args.preferred_item_id,
            )
        except TodoError as exc:
            return _render_error(exc)
        if claim is None:
            return ToolResult(
                content='{"status":"idle","reason":"no ready Todo task"}'
            )
        snapshot = self.service.snapshot()
        item_map = snapshot.item_map if snapshot is not None else None
        return ToolResult(
            content=json.dumps(
                {
                    "status": "claimed",
                    "claim": claim.to_dict(item_map),
                    "todo": snapshot.render(max_chars=8_000) if snapshot is not None else None,
                },
                ensure_ascii=False,
            )
        )


class TodoHeartbeatParams(BaseModel):
    lease_seconds: float = Field(default=30.0, gt=0, le=3_600)


class TodoHeartbeatTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_heartbeat"
    description: ClassVar[str] = "Renew all Todo task leases held by this Worker."
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoHeartbeatParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoHeartbeatParams)
        if error := self._check_context(ctx):
            return error
        if not ctx.todo_owner_id:
            return ToolResult(
                content="[todo_heartbeat_error] only a Worker can renew a Todo lease",
                is_error=True,
            )
        renewed = self.service.renew_owner(
            ctx.todo_owner_id,
            lease_seconds=args.lease_seconds,
        )
        return ToolResult(
            content=json.dumps(
                {"status": "renewed", "tasks": renewed}, ensure_ascii=False
            )
        )


class TodoReleaseParams(BaseModel):
    requeue: bool = True


class TodoReleaseTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_release"
    description: ClassVar[str] = (
        "Release this Worker's claimed Todo tasks. Requeue unfinished work by default."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoReleaseParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoReleaseParams)
        if error := self._check_context(ctx):
            return error
        if not ctx.todo_owner_id:
            return ToolResult(
                content="[todo_release_error] only a Worker can release Todo tasks",
                is_error=True,
            )
        released = self.service.release_owner(ctx.todo_owner_id, requeue=args.requeue)
        return ToolResult(
            content=json.dumps(
                {"status": "released", "item_ids": list(released)},
                ensure_ascii=False,
            )
        )


class TodoAddParams(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4_000)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    expected_version: int = Field(ge=0)


class TodoAddTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_add"
    description: ClassVar[str] = (
        "Add one Todo task. Use the plan_version returned by todo_init or todo_list "
        "as expected_version; dependencies must be existing task IDs."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoAddParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoAddParams)
        if error := self._check_context(ctx):
            return error
        try:
            snapshot = self.service.add(
                args.title,
                description=args.description,
                dependencies=args.dependencies,
                expected_version=args.expected_version,
            )
            return ToolResult(content=_render_snapshot(snapshot))
        except TodoError as exc:
            return _render_error(exc)


class TodoUpdateParams(BaseModel):
    item_id: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=240)
    description: str | None = Field(default=None, max_length=4_000)
    status: TodoStatus | None = None
    dependencies: list[str] | None = Field(default=None, max_length=20)
    expected_version: int = Field(ge=0)
    expected_revision: int | None = Field(default=None, ge=0)


class TodoUpdateTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_update"
    description: ClassVar[str] = (
        "Update a Todo task's title, description, status, or dependencies. Use "
        "completed only after the task is actually done."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoUpdateParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoUpdateParams)
        if error := self._check_context(ctx):
            return error
        try:
            snapshot = self.service.update(
                args.item_id,
                title=args.title,
                description=args.description,
                status=args.status,
                dependencies=args.dependencies,
                expected_version=args.expected_version,
                expected_revision=args.expected_revision,
                owner_id=ctx.todo_owner_id,
            )
            return ToolResult(content=_render_snapshot(snapshot))
        except TodoError as exc:
            return _render_error(exc)


class TodoRemoveParams(BaseModel):
    item_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)


class TodoRemoveTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_remove"
    description: ClassVar[str] = (
        "Remove a Todo task only when no other task depends on it. The operation "
        "is rejected when dependents exist."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoRemoveParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoRemoveParams)
        if error := self._check_context(ctx):
            return error
        try:
            snapshot = self.service.remove(args.item_id, expected_version=args.expected_version)
            return ToolResult(content=_render_snapshot(snapshot))
        except TodoError as exc:
            return _render_error(exc)


class TodoCheckpointParams(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=0)


class TodoCheckpointTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_checkpoint"
    description: ClassVar[str] = (
        "Save a named Todo-only checkpoint. It does not snapshot or roll back files, "
        "Shell commands, Git state, or other external side effects."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoCheckpointParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoCheckpointParams)
        if error := self._check_context(ctx):
            return error
        try:
            checkpoint = self.service.create_checkpoint(
                args.label,
                expected_version=args.expected_version,
            )
            return ToolResult(
                content=(
                    f"Checkpoint created: {checkpoint.id}\n"
                    f"label={checkpoint.label}; source_version={checkpoint.source_plan_version}"
                )
            )
        except TodoError as exc:
            return _render_error(exc)


class TodoCheckpointsParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class TodoCheckpointsTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_checkpoints"
    description: ClassVar[str] = "List named Todo-only checkpoints for this session."
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = TodoCheckpointsParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoCheckpointsParams)
        if error := self._check_context(ctx):
            return error
        try:
            checkpoints = self.service.list_checkpoints(limit=args.limit)
        except TodoError as exc:
            return _render_error(exc)
        if not checkpoints:
            return ToolResult(content="No Todo checkpoints.")
        lines = ["Todo checkpoints:"]
        lines.extend(
            f"- [{checkpoint.id}] {checkpoint.label} (plan version {checkpoint.source_plan_version})"
            for checkpoint in checkpoints
        )
        return ToolResult(content="\n".join(lines))


class TodoRollbackParams(BaseModel):
    checkpoint_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)


class TodoRollbackTool(TodoTool, Tool):
    name: ClassVar[str] = "todo_rollback"
    description: ClassVar[str] = (
        "Restore Todo tasks and dependencies from a checkpoint. This only changes "
        "Todo state; it never undoes file, Shell, Git, or external side effects."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = TodoRollbackParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, TodoRollbackParams)
        if error := self._check_context(ctx):
            return error
        try:
            snapshot = self.service.rollback(
                args.checkpoint_id,
                expected_version=args.expected_version,
            )
            return ToolResult(content=_render_snapshot(snapshot))
        except TodoError as exc:
            return _render_error(exc)


def register_todo_tools(registry: ToolRegistry, service: TodoService) -> None:
    """Register the stable Todo tool set without replacing caller tools."""
    tools = (
        TodoInitTool(service),
        TodoListTool(service),
        TodoClaimTool(service),
        TodoHeartbeatTool(service),
        TodoReleaseTool(service),
        TodoAddTool(service),
        TodoUpdateTool(service),
        TodoRemoveTool(service),
        TodoCheckpointTool(service),
        TodoCheckpointsTool(service),
        TodoRollbackTool(service),
    )
    for tool in tools:
        if tool.name not in registry:
            registry.register(tool)
