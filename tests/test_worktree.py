"""Integration tests for isolated Implement worktrees."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from carbide.agents.domain import SubagentRequest
from carbide.agents.worktree import WorktreeError, WorktreeManager
from carbide.providers.base import Provider, StreamEnd, StreamTextDelta, StreamToolUse
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class ImplementProvider(Provider):
    """Write one file in the child, then return a final answer."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "implement-test"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        del messages, system, tools, max_tokens, temperature, kwargs
        self.calls += 1
        if self.calls == 1:
            yield StreamToolUse(
                "write-1",
                "write",
                {"path": "app.py", "content": "value = 2\n"},
            )
            yield StreamEnd(
                stop_reason="tool_use",
                assistant_content=[
                    {
                        "type": "tool_use",
                        "id": "write-1",
                        "name": "write",
                        "input": {"path": "app.py", "content": "value = 2\n"},
                    }
                ],
            )
            return
        yield StreamTextDelta("implemented")
        yield StreamEnd()

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        del messages, system, tools
        return 1

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Carbide Tests")
    _git(tmp_path, "config", "user.email", "carbide-tests@example.invalid")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


@pytest.mark.asyncio
async def test_worktree_changes_are_committed_and_merged(repository: Path) -> None:
    manager = WorktreeManager(repository)
    handle = await manager.prepare("run-merge")
    (handle.workspace / "app.py").write_text("value = 2\n", encoding="utf-8")

    outcome = await manager.finalize(handle, commit_message="carbide: implement change")

    assert outcome.status == "merged"
    assert outcome.commit
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not handle.path.exists()
    assert _git(repository, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_dirty_parent_is_rejected_before_worktree_creation(repository: Path) -> None:
    (repository / "local.py").write_text("local = True\n", encoding="utf-8")
    manager = WorktreeManager(repository)

    with pytest.raises(WorktreeError, match="clean parent"):
        await manager.prepare("run-dirty")


@pytest.mark.asyncio
async def test_parent_change_blocks_merge_without_partial_state(repository: Path) -> None:
    manager = WorktreeManager(repository)
    handle = await manager.prepare("run-race")
    (handle.workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "app.py").write_text("value = parent\n", encoding="utf-8")

    outcome = await manager.finalize(handle, commit_message="carbide: conflicting change")

    assert outcome.status == "merge_blocked"
    assert outcome.commit
    assert "parent" in (repository / "app.py").read_text(encoding="utf-8")
    assert "UU" not in _git(repository, "status", "--porcelain")
    assert not handle.path.exists()


@pytest.mark.asyncio
async def test_parallel_worktree_changes_rebase_before_merge(repository: Path) -> None:
    manager = WorktreeManager(repository)
    first = await manager.prepare("run-first")
    second = await manager.prepare("run-second")
    (first.workspace / "first.py").write_text("first = True\n", encoding="utf-8")
    (second.workspace / "second.py").write_text("second = True\n", encoding="utf-8")

    first_outcome = await manager.finalize(first, commit_message="first change")
    second_outcome = await manager.finalize(second, commit_message="second change")

    assert first_outcome.status == "merged"
    assert second_outcome.status == "merged"
    assert (repository / "first.py").exists()
    assert (repository / "second.py").exists()


@pytest.mark.asyncio
async def test_implement_subagent_writes_and_merges(repository: Path) -> None:
    agent = Agent(
        provider=ImplementProvider(),
        project_dir=repository,
        tool_registry=create_default_registry(),
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
    )
    assert agent.subagent_manager is not None

    result = await agent.subagent_manager.dispatch(
        SubagentRequest(task="claim and complete the current Todo task")
    )

    assert result.status.value == "completed"
    assert result.metadata["worktree_status"] == "merged"
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert _git(repository, "status", "--porcelain").startswith("?? .carbide")
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_implement_subagent_rejects_dirty_parent(repository: Path) -> None:
    (repository / "local.py").write_text("local = True\n", encoding="utf-8")
    agent = Agent(
        provider=ImplementProvider(),
        project_dir=repository,
        tool_registry=create_default_registry(),
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
    )
    assert agent.subagent_manager is not None

    result = await agent.subagent_manager.dispatch(
        SubagentRequest(task="claim and complete the current Todo task")
    )

    assert result.status.value == "failed"
    assert "clean parent" in (result.error or "")
    await agent.__aexit__(None, None, None)
