"""Tests for hook system."""

from __future__ import annotations

import pytest

from carbide.governance.hooks import HookContext, HookRegistry
from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from pydantic import BaseModel


class DummyParams(BaseModel):
    value: str


class DummyTool(Tool):
    name = "dummy"
    description = "A test tool"
    read_only = True
    destructive = False
    concurrency_safe = True
    risk_level = RiskLevel.SAFE
    params = DummyParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=f"executed: {args.value}")


def test_hooks_registry_starts_empty():
    """New registry has no hooks."""
    registry = HookRegistry()
    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")

    # No before hooks means no short-circuit
    assert registry.run_before(tool, ctx) is None

    # No after hooks means result passes through unchanged
    original = ToolResult(content="original")
    assert registry.run_after(tool, ctx, original) is original


def test_before_hook_can_short_circuit():
    """Before hook can prevent execution by returning a result."""
    registry = HookRegistry()

    def block_all(tool: Tool, ctx: HookContext) -> ToolResult | None:
        return ToolResult(content="blocked by hook", is_error=True)

    registry.register_before(block_all)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")
    result = registry.run_before(tool, ctx)

    assert result is not None
    assert result.content == "blocked by hook"
    assert result.is_error is True


def test_before_hook_can_allow_execution():
    """Before hook returning None allows execution to proceed."""
    registry = HookRegistry()

    def allow_all(tool: Tool, ctx: HookContext) -> ToolResult | None:
        return None

    registry.register_before(allow_all)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")
    result = registry.run_before(tool, ctx)

    assert result is None


def test_multiple_before_hooks_run_in_order():
    """Before hooks run in registration order, first short-circuit wins."""
    registry = HookRegistry()
    executed = []

    def hook1(tool: Tool, ctx: HookContext) -> ToolResult | None:
        executed.append("hook1")
        return None  # Allow

    def hook2(tool: Tool, ctx: HookContext) -> ToolResult | None:
        executed.append("hook2")
        return ToolResult(content="blocked by hook2")

    def hook3(tool: Tool, ctx: HookContext) -> ToolResult | None:
        executed.append("hook3")
        return None

    registry.register_before(hook1)
    registry.register_before(hook2)
    registry.register_before(hook3)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")
    result = registry.run_before(tool, ctx)

    # hook1 and hook2 ran, hook3 didn't because hook2 short-circuited
    assert executed == ["hook1", "hook2"]
    assert result is not None
    assert result.content == "blocked by hook2"


def test_after_hook_can_modify_result():
    """After hook can transform the result."""
    registry = HookRegistry()

    def uppercase(tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult:
        return ToolResult(content=result.content.upper(), is_error=result.is_error)

    registry.register_after(uppercase)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")
    original = ToolResult(content="hello world")
    modified = registry.run_after(tool, ctx, original)

    assert modified.content == "HELLO WORLD"


def test_multiple_after_hooks_chain():
    """After hooks run in order, each receiving the previous result."""
    registry = HookRegistry()

    def add_prefix(tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult:
        return ToolResult(content=f"[prefix] {result.content}", is_error=result.is_error)

    def add_suffix(tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult:
        return ToolResult(content=f"{result.content} [suffix]", is_error=result.is_error)

    registry.register_after(add_prefix)
    registry.register_after(add_suffix)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")
    original = ToolResult(content="content")
    final = registry.run_after(tool, ctx, original)

    assert final.content == "[prefix] content [suffix]"


def test_clear_removes_all_hooks():
    """Clear removes both before and after hooks."""
    registry = HookRegistry()

    def before(tool: Tool, ctx: HookContext) -> ToolResult | None:
        return ToolResult(content="blocked")

    def after(tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult:
        return ToolResult(content="modified")

    registry.register_before(before)
    registry.register_after(after)
    registry.clear()

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")

    # No hooks means passthrough behavior
    assert registry.run_before(tool, ctx) is None

    original = ToolResult(content="original")
    assert registry.run_after(tool, ctx, original) is original


def test_hook_context_carries_tool_info():
    """HookContext provides tool name, args, and workspace."""
    registry = HookRegistry()
    captured_ctx = None

    def capture(tool: Tool, ctx: HookContext) -> ToolResult | None:
        nonlocal captured_ctx
        captured_ctx = ctx
        return None

    registry.register_before(capture)

    tool = DummyTool()
    args = DummyParams(value="testval")
    ctx = HookContext(tool_name="dummy", arguments=args, workspace="/workspace")
    registry.run_before(tool, ctx)

    assert captured_ctx is not None
    assert captured_ctx.tool_name == "dummy"
    assert captured_ctx.arguments.value == "testval"
    assert captured_ctx.workspace == "/workspace"


def test_error_hook_is_called_on_exception():
    """Error hooks are triggered when tool execution fails."""
    registry = HookRegistry()
    captured_error = None

    def capture_error(tool: Tool, ctx: HookContext, error: Exception) -> None:
        nonlocal captured_error
        captured_error = error

    registry.register_error(capture_error)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")
    test_error = ValueError("test error")

    registry.run_error(tool, ctx, test_error)

    assert captured_error is test_error


def test_error_hooks_are_isolated():
    """If one error hook fails, others still run."""
    registry = HookRegistry()
    executed = []

    def failing_hook(tool: Tool, ctx: HookContext, error: Exception) -> None:
        executed.append("failing")
        raise RuntimeError("hook crashed")

    def working_hook(tool: Tool, ctx: HookContext, error: Exception) -> None:
        executed.append("working")

    registry.register_error(failing_hook)
    registry.register_error(working_hook)

    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")

    # Should not raise, both hooks attempted
    registry.run_error(tool, ctx, ValueError("test"))

    assert "failing" in executed
    assert "working" in executed


def test_api_hooks_receive_messages():
    """API hooks receive message lists."""
    registry = HookRegistry()
    captured_messages = None

    def capture_pre(messages: list) -> None:
        nonlocal captured_messages
        captured_messages = messages

    registry.register_pre_api(capture_pre)

    test_messages = [{"role": "user", "content": "hello"}]
    registry.run_pre_api(test_messages)

    assert captured_messages == test_messages


def test_multiple_api_hooks_run_in_order():
    """Multiple API hooks execute in registration order."""
    registry = HookRegistry()
    executed = []

    def hook1(messages: list) -> None:
        executed.append("hook1")

    def hook2(messages: list) -> None:
        executed.append("hook2")

    registry.register_pre_api(hook1)
    registry.register_pre_api(hook2)

    registry.run_pre_api([])

    assert executed == ["hook1", "hook2"]


def test_clear_removes_all_hook_types():
    """Clear removes all hook types including error and API hooks."""
    registry = HookRegistry()

    registry.register_before(lambda t, c: None)
    registry.register_after(lambda t, c, r: r)
    registry.register_error(lambda t, c, e: None)
    registry.register_pre_api(lambda m: None)
    registry.register_post_api(lambda m: None)

    registry.clear()

    # All hook lists should be empty (no exceptions or side effects)
    tool = DummyTool()
    ctx = HookContext(tool_name="dummy", arguments=DummyParams(value="test"), workspace="/tmp")

    assert registry.run_before(tool, ctx) is None
    assert registry.run_after(tool, ctx, ToolResult(content="test")) is not None
    registry.run_error(tool, ctx, ValueError("test"))  # Should not raise
    registry.run_pre_api([])  # Should not raise
    registry.run_post_api([])  # Should not raise

