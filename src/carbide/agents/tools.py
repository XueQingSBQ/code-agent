"""Model-facing tools for the direct parent-child Agent channel."""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from carbide.agents.domain import SubagentRequest
from carbide.agents.manager import SubagentManager
from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.registry import ToolRegistry


class AgentSpawnParams(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    background: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0, le=1_800)


class AgentSpawnTool(Tool):
    name: ClassVar[str] = "agent"
    description: ClassVar[str] = (
        "Create one generic Worker for the current session Todo plan. The Worker "
        "chooses and claims a ready task itself, executes it in an isolated Git "
        "worktree, and updates the shared Todo state. Use background=true for "
        "long-running work."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = AgentSpawnParams

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, AgentSpawnParams)
        request = SubagentRequest(
            task=args.task,
            background=args.background,
            timeout_seconds=args.timeout_seconds,
        )
        result = await self.manager.dispatch(request)
        return ToolResult(
            content=result.to_tool_result(),
            is_error=result.status.value in {"failed", "merge_blocked"},
        )


class AgentStatusParams(BaseModel):
    agent_id: str | None = None
    status: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class AgentStatusTool(Tool):
    name: ClassVar[str] = "agent_status"
    description: ClassVar[str] = "List or inspect direct child Agent execution state."
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = AgentStatusParams

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, AgentStatusParams)
        if args.agent_id:
            value: Any = self.manager.status(args.agent_id)
        else:
            value = self.manager.list_runs(status=args.status)[: args.limit]
        return ToolResult(content=json.dumps(value, ensure_ascii=False))


class AgentSendParams(BaseModel):
    agent_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=20_000)
    kind: str = Field(default="plan_update", min_length=1, max_length=80)


class AgentSendTool(Tool):
    name: ClassVar[str] = "agent_send"
    description: ClassVar[str] = "Send a plan update or answer to a running direct child Agent."
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = AgentSendParams

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, AgentSendParams)
        try:
            message_id = self.manager.send(
                args.agent_id,
                kind=args.kind,
                content=args.message,
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=json.dumps(
                {"status": "queued", "agent_id": args.agent_id, "message_id": message_id},
                ensure_ascii=False,
            )
        )


class AgentCancelParams(BaseModel):
    agent_id: str = Field(min_length=1)


class AgentCancelTool(Tool):
    name: ClassVar[str] = "agent_cancel"
    description: ClassVar[str] = "Cancel a running direct child Agent."
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = AgentCancelParams

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, AgentCancelParams)
        cancelled = await self.manager.cancel(args.agent_id)
        return ToolResult(
            content=json.dumps(
                {"agent_id": args.agent_id, "cancelled": cancelled}, ensure_ascii=False
            )
        )


class AgentMessageParams(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    kind: Literal["progress", "question"] = "progress"
    wait_for_answer: bool = False
    timeout_seconds: float = Field(default=300.0, gt=0, le=1_800)


class AgentMessageTool(Tool):
    """Child-only tool for durable progress reports and parent questions."""

    name: ClassVar[str] = "agent_message"
    description: ClassVar[str] = (
        "Report progress to the parent Agent, or ask it a question. For a "
        "question in a background child, set wait_for_answer=true to pause "
        "until the parent sends agent_send."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = AgentMessageParams

    def __init__(self, manager: SubagentManager, run_id: str) -> None:
        self.manager = manager
        self.run_id = run_id

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, AgentMessageParams)
        try:
            record = self.manager.status(self.run_id)
            can_wait = bool(record and record["background"])
            message_id = self.manager.child_message(
                self.run_id,
                kind=args.kind,
                content=args.message,
                wait_for_answer=args.wait_for_answer,
                timeout_seconds=args.timeout_seconds,
            )
            answered: bool | None = None
            if args.wait_for_answer and can_wait:
                answered = await self.manager.wait_for_parent(
                    self.run_id,
                    timeout_seconds=args.timeout_seconds,
                )
            return ToolResult(
                content=json.dumps(
                    {
                        "status": "delivered" if answered else "queued",
                        "message_id": message_id,
                        "awaited": bool(args.wait_for_answer and can_wait),
                        "answered": answered,
                    },
                    ensure_ascii=False,
                )
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)


def register_subagent_tools(registry: ToolRegistry, manager: SubagentManager) -> None:
    """Register the parent-only direct child control tools once."""
    for tool in (
        AgentSpawnTool(manager),
        AgentStatusTool(manager),
        AgentSendTool(manager),
        AgentCancelTool(manager),
    ):
        if tool.name not in registry:
            registry.register(tool)
