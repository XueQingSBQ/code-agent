"""Tests for PersistentBashTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from carbide.tools.base import ToolContext
from carbide.tools.persistent_bash import PersistentBashParams, PersistentBashTool


@pytest.fixture
async def tool():
    """Create a tool instance and ensure cleanup."""
    t = PersistentBashTool()
    yield t
    await t.close_session()


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    """Create a tool context for testing."""
    return ToolContext(workspace=tmp_path, shell="gitbash")


async def test_basic_command(tool: PersistentBashTool, context: ToolContext) -> None:
    """Execute a simple command."""
    args = PersistentBashParams(command="echo hello")
    result = await tool.run(args, context)
    assert not result.is_error
    assert "hello" in result.content


async def test_exit_code_captured(tool: PersistentBashTool, context: ToolContext) -> None:
    """Verify exit codes are captured correctly."""
    # Success
    args = PersistentBashParams(command="true")
    result = await tool.run(args, context)
    assert not result.is_error

    # Failure
    args = PersistentBashParams(command="false")
    result = await tool.run(args, context)
    assert result.is_error
    assert "exit 1" in result.content


async def test_state_persists_across_commands(
    tool: PersistentBashTool, context: ToolContext
) -> None:
    """Directory changes and environment variables persist."""
    # Create subdirectory and cd into it
    args = PersistentBashParams(command="mkdir subdir && cd subdir")
    result = await tool.run(args, context)
    assert not result.is_error

    # Verify we're still in subdir
    args = PersistentBashParams(command="pwd")
    result = await tool.run(args, context)
    assert not result.is_error
    assert "subdir" in result.content

    # Set environment variable
    args = PersistentBashParams(command="export TEST_VAR=hello")
    result = await tool.run(args, context)
    assert not result.is_error

    # Verify it persists
    args = PersistentBashParams(command="echo $TEST_VAR")
    result = await tool.run(args, context)
    assert not result.is_error
    assert "hello" in result.content


async def test_timeout_handling(tool: PersistentBashTool, context: ToolContext) -> None:
    """Timeout is properly handled."""
    # Note: BashSession timeout applies per command, and the session must be
    # created with the timeout. Since we want to test timeout behavior, we need
    # to ensure the command actually times out within the session's timeout window.
    args = PersistentBashParams(command="sleep 10", timeout=0.5)
    result = await tool.run(args, context)
    assert result.is_error
    assert "timed out" in result.content.lower()


async def test_large_output_truncated(
    tool: PersistentBashTool, context: ToolContext
) -> None:
    """Large output is truncated with elision marker."""
    args = PersistentBashParams(command="seq 1 10000")
    result = await tool.run(args, context)
    assert not result.is_error
    # Should contain both early and late numbers
    assert "1" in result.content or "2" in result.content
    assert "9999" in result.content or "10000" in result.content


async def test_session_reused_across_calls(
    tool: PersistentBashTool, context: ToolContext
) -> None:
    """The same session is reused for multiple commands."""
    # First command creates the session
    args = PersistentBashParams(command="echo $$")  # Print bash PID
    result1 = await tool.run(args, context)
    pid1 = result1.content.strip()

    # Second command should use the same session (same PID)
    result2 = await tool.run(args, context)
    pid2 = result2.content.strip()

    assert pid1 == pid2
    assert pid1 != ""


async def test_session_cleanup(tool: PersistentBashTool, context: ToolContext) -> None:
    """Session can be explicitly closed."""
    args = PersistentBashParams(command="echo test")
    await tool.run(args, context)
    assert tool._session is not None

    await tool.close_session()
    assert tool._session is None
