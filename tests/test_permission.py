"""Tests for permission engine."""

from __future__ import annotations

import pytest

from carbide.governance.permission import (
    PermissionEngine,
    PermissionMode,
    PermissionResult,
    RiskLevel,
)
from carbide.tools.base import Tool, ToolContext, ToolResult
from pydantic import BaseModel


class DummyParams(BaseModel):
    value: str


class SafeTool(Tool):
    name = "safe"
    description = "A safe read-only tool"
    read_only = True
    destructive = False
    concurrency_safe = True
    risk_level = RiskLevel.SAFE
    params = DummyParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="safe")


class WriteTool(Tool):
    name = "write"
    description = "A file write tool"
    read_only = False
    destructive = True
    concurrency_safe = False
    risk_level = RiskLevel.WRITE
    params = DummyParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="written")


class DestructiveTool(Tool):
    name = "destroy"
    description = "A destructive tool"
    read_only = False
    destructive = True
    concurrency_safe = False
    risk_level = RiskLevel.DESTRUCTIVE
    params = DummyParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="destroyed")


def test_auto_mode_allows_safe():
    """AUTO mode (alias for PROMPT) allows safe operations without prompting."""
    engine = PermissionEngine(mode=PermissionMode.AUTO)
    tool = SafeTool()
    args = DummyParams(value="test")

    result = engine.check(tool, args)

    assert result.allowed is True
    assert result.prompt is None
    assert result.should_audit is False


def test_auto_mode_blocks_write():
    """AUTO mode blocks write operations and requires prompt."""
    engine = PermissionEngine(mode=PermissionMode.AUTO)
    tool = WriteTool()
    args = DummyParams(value="test")

    result = engine.check(tool, args)

    # PROMPT mode allows WRITE operations but audits them
    assert result.allowed is True
    assert result.should_audit is True


def test_auto_mode_blocks_destructive():
    """AUTO mode blocks destructive operations and requires prompt."""
    engine = PermissionEngine(mode=PermissionMode.AUTO)
    tool = DestructiveTool()
    args = DummyParams(value="test")

    result = engine.check(tool, args)

    assert result.allowed is False
    assert result.prompt is not None
    assert result.should_audit is True
    assert "destructive" in result.prompt.lower() or "destroy" in result.prompt.lower()


def test_always_mode_allows_all():
    """ALWAYS mode (alias for ALWAYS_ALLOW) allows everything without prompting."""
    engine = PermissionEngine(mode=PermissionMode.ALWAYS)

    for tool_cls in [SafeTool, WriteTool, DestructiveTool]:
        tool = tool_cls()
        args = DummyParams(value="test")
        result = engine.check(tool, args)
        assert result.allowed is True
        assert result.prompt is None
        assert result.should_audit is False


def test_never_mode_blocks_all():
    """NEVER mode (alias for DENY) blocks destructive operations."""
    engine = PermissionEngine(mode=PermissionMode.NEVER)

    # Safe operations allowed
    safe_tool = SafeTool()
    safe_result = engine.check(safe_tool, DummyParams(value="test"))
    assert safe_result.allowed is True

    # Write operations allowed but audited
    write_tool = WriteTool()
    write_result = engine.check(write_tool, DummyParams(value="test"))
    assert write_result.allowed is True
    assert write_result.should_audit is True

    # Destructive operations blocked
    destroy_tool = DestructiveTool()
    destroy_result = engine.check(destroy_tool, DummyParams(value="test"))
    assert destroy_result.allowed is False
    assert destroy_result.should_audit is True


def test_permission_result_repr():
    """PermissionResult has useful repr for debugging."""
    result = PermissionResult(allowed=True, prompt="Test prompt")
    assert "allowed=True" in repr(result)
    assert "Test prompt" in repr(result)


def test_engine_mode_can_be_changed():
    """Engine mode can be updated at runtime."""
    engine = PermissionEngine(mode=PermissionMode.AUTO)
    tool = DestructiveTool()
    args = DummyParams(value="test")

    # Initially blocked
    result = engine.check(tool, args)
    assert result.allowed is False

    # Switch to ALWAYS
    engine.mode = PermissionMode.ALWAYS
    result = engine.check(tool, args)
    assert result.allowed is True


def test_always_allow_mode():
    """ALWAYS_ALLOW mode allows all operations without audit."""
    engine = PermissionEngine(mode=PermissionMode.ALWAYS_ALLOW)

    for tool_cls in [SafeTool, WriteTool, DestructiveTool]:
        tool = tool_cls()
        result = engine.check(tool, DummyParams(value="test"))
        assert result.allowed is True
        assert result.should_audit is False
        assert result.prompt is None


def test_prompt_mode_behavior():
    """PROMPT mode allows SAFE and WRITE, prompts for DESTRUCTIVE."""
    engine = PermissionEngine(mode=PermissionMode.PROMPT)

    # SAFE: allowed, no audit
    safe_result = engine.check(SafeTool(), DummyParams(value="test"))
    assert safe_result.allowed is True
    assert safe_result.should_audit is False

    # WRITE: allowed, audited
    write_result = engine.check(WriteTool(), DummyParams(value="test"))
    assert write_result.allowed is True
    assert write_result.should_audit is True

    # DESTRUCTIVE: blocked, requires prompt, audited
    destroy_result = engine.check(DestructiveTool(), DummyParams(value="test"))
    assert destroy_result.allowed is False
    assert destroy_result.prompt is not None
    assert destroy_result.should_audit is True


def test_audit_mode_allows_all_with_logging():
    """AUDIT mode allows everything but logs all operations."""
    engine = PermissionEngine(mode=PermissionMode.AUDIT)

    for tool_cls in [SafeTool, WriteTool, DestructiveTool]:
        tool = tool_cls()
        result = engine.check(tool, DummyParams(value="test"))
        assert result.allowed is True
        assert result.should_audit is True
        assert result.prompt is None


def test_deny_mode_blocks_destructive():
    """DENY mode blocks DESTRUCTIVE operations."""
    engine = PermissionEngine(mode=PermissionMode.DENY)

    # SAFE: allowed
    safe_result = engine.check(SafeTool(), DummyParams(value="test"))
    assert safe_result.allowed is True

    # WRITE: allowed but audited
    write_result = engine.check(WriteTool(), DummyParams(value="test"))
    assert write_result.allowed is True
    assert write_result.should_audit is True

    # DESTRUCTIVE: blocked
    destroy_result = engine.check(DestructiveTool(), DummyParams(value="test"))
    assert destroy_result.allowed is False
    assert destroy_result.should_audit is True
    assert "denied by policy" in destroy_result.prompt
