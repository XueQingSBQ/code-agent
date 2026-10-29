"""Streaming executor: ordering, concurrency and failure isolation."""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

import pytest
from pydantic import BaseModel

from carbide.engine.executor import StreamingToolExecutor
from carbide.execution.metrics import MetricsCollector
from carbide.governance.permission import PermissionEngine, PermissionMode, RiskLevel
from carbide.providers.base import StreamToolUse
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.registry import ToolRegistry


class SleepParams(BaseModel):
    seconds: float = 0.05
    label: str = ""


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, float]] = []

    def mark(self, phase: str, label: str) -> None:
        self.events.append((phase, label, time.perf_counter()))


RECORDER = _Recorder()


class ParallelTool(Tool):
    name: ClassVar[str] = "par"
    description: ClassVar[str] = "sleeps, safe to parallelise"
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = SleepParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SleepParams)
        RECORDER.mark("start", args.label)
        await asyncio.sleep(args.seconds)
        RECORDER.mark("end", args.label)
        return ToolResult(content=f"par:{args.label}")


class SerialTool(Tool):
    name: ClassVar[str] = "ser"
    description: ClassVar[str] = "mutates, must be serialised"
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = True
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.DESTRUCTIVE
    params: ClassVar[type[BaseModel]] = SleepParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SleepParams)
        RECORDER.mark("start", args.label)
        await asyncio.sleep(args.seconds)
        RECORDER.mark("end", args.label)
        return ToolResult(content=f"ser:{args.label}")


class ExplodingTool(Tool):
    name: ClassVar[str] = "boom"
    description: ClassVar[str] = "raises"
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = SleepParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("tool exploded")


@pytest.fixture(autouse=True)
def _reset_recorder() -> None:
    RECORDER.events.clear()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all([ParallelTool(), SerialTool(), ExplodingTool()])
    return reg


def _call(name: str, label: str, seconds: float = 0.05) -> StreamToolUse:
    return StreamToolUse(
        id=f"toolu_{label}", name=name, arguments={"seconds": seconds, "label": label}
    )


async def test_safe_tools_overlap(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    executor = StreamingToolExecutor(registry, tool_ctx)
    started = time.perf_counter()
    for label in ("a", "b", "c"):
        executor.add_tool(_call("par", label))
    completed = await executor.gather_remaining()
    elapsed = time.perf_counter() - started

    assert [c.result.content for c in completed] == ["par:a", "par:b", "par:c"]
    # Three 50ms sleeps in parallel finish well under the 150ms serial cost.
    assert elapsed < 0.13, f"tools did not overlap (took {elapsed:.3f}s)"


async def test_unsafe_tool_acts_as_a_barrier(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    executor = StreamingToolExecutor(registry, tool_ctx)
    executor.add_tool(_call("par", "before"))
    executor.add_tool(_call("ser", "writer"))
    executor.add_tool(_call("par", "after"))
    await executor.gather_remaining()

    def at(phase: str, label: str) -> float:
        return next(t for p, lbl, t in RECORDER.events if p == phase and lbl == label)

    # The writer waits for what was already running, and the tool dispatched
    # after it waits for the writer.
    assert at("start", "writer") >= at("end", "before")
    assert at("start", "after") >= at("end", "writer")


async def test_results_keep_dispatch_order_not_completion_order(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    executor = StreamingToolExecutor(registry, tool_ctx)
    executor.add_tool(_call("par", "slow", seconds=0.08))
    executor.add_tool(_call("par", "fast", seconds=0.01))
    completed = await executor.gather_remaining()
    assert [c.call.id for c in completed] == ["toolu_slow", "toolu_fast"]


async def test_a_crashing_tool_still_produces_a_result(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    """A raised exception must become an error result, not escape the executor.

    If it escaped, the tool_use would be left unanswered and the next request
    would be rejected outright.
    """
    executor = StreamingToolExecutor(registry, tool_ctx)
    executor.add_tool(_call("boom", "x"))
    executor.add_tool(_call("par", "y"))
    completed = await executor.gather_remaining()

    assert len(completed) == 2
    assert completed[0].result.is_error
    assert "tool exploded" in completed[0].result.content
    # The sibling was unaffected — no TaskGroup-style cancellation.
    assert completed[1].result.content == "par:y"


async def test_executor_records_metrics_once_after_harvest(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    collector = MetricsCollector()
    executor = StreamingToolExecutor(registry, tool_ctx, metrics_collector=collector)
    executor.add_tool(_call("par", "measured", seconds=0.01))
    executor.add_tool(_call("boom", "failed"))

    completed = await executor.gather_remaining()
    await executor.gather_remaining()

    assert len(completed) == 2
    assert collector.get_summary()["recorded_executions"] == 2
    assert collector.get_tool_stats("par")["count"] == 1
    assert collector.get_error_summary() == {"RuntimeError": 1}


async def test_abort_answers_every_dispatched_call(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    executor = StreamingToolExecutor(registry, tool_ctx)
    for label in ("a", "b"):
        executor.add_tool(_call("par", label, seconds=5.0))
    completed = await executor.abort()
    assert len(completed) == 2
    assert all(c.result.is_error for c in completed)
    blocks = executor.to_result_blocks(completed)
    assert {b["tool_use_id"] for b in blocks} == {"toolu_a", "toolu_b"}


async def test_permission_denial_returns_policy_reason(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    """A denied destructive tool must produce a result, not an executor crash."""
    executor = StreamingToolExecutor(
        registry,
        tool_ctx,
        permission_engine=PermissionEngine(mode=PermissionMode.PROMPT),
    )
    executor.add_tool(_call("ser", "blocked"))

    completed = await executor.gather_remaining()

    assert len(completed) == 1
    assert completed[0].result.is_error
    assert "requires approval" in completed[0].result.content
    assert "PermissionResult" not in completed[0].result.content


async def test_permission_callback_can_approve_destructive_tool(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    approvals: list[str] = []

    async def approve(call: StreamToolUse, prompt: str) -> bool:
        approvals.append(prompt)
        return True

    executor = StreamingToolExecutor(
        registry,
        tool_ctx,
        permission_engine=PermissionEngine(mode=PermissionMode.PROMPT),
        approval_callback=approve,
    )
    executor.add_tool(_call("ser", "approved", seconds=0.01))

    completed = await executor.gather_remaining()

    assert completed[0].result.content == "ser:approved"
    assert len(approvals) == 1


async def test_deny_mode_cannot_be_overridden_by_approval_callback(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    executor = StreamingToolExecutor(
        registry,
        tool_ctx,
        permission_engine=PermissionEngine(mode=PermissionMode.DENY),
        approval_callback=lambda call, prompt: True,
    )
    executor.add_tool(_call("ser", "hard-denied"))

    completed = await executor.gather_remaining()

    assert completed[0].result.is_error
    assert "denied by policy" in completed[0].result.content
