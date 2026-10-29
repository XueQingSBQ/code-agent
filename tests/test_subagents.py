"""Production contracts for the direct parent-child Agent channel."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from carbide.agents.domain import SubagentRequest
from carbide.agents.tools import AgentSpawnTool
from carbide.governance import SessionStore
from carbide.providers.base import (
    Provider,
    ProviderError,
    StreamEnd,
    StreamTextDelta,
    StreamToolUse,
)
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


class MessageProvider(Provider):
    """Ask the parent once, then finish after the next model boundary."""

    def __init__(self, *, wait_for_answer: bool = False) -> None:
        self.wait_for_answer = wait_for_answer
        self.calls = 0

    @property
    def model(self) -> str:
        return "subagent-test"

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
                "message-1",
                "agent_message",
                {
                    "kind": "question",
                    "message": "Should I include generated files?",
                    "wait_for_answer": self.wait_for_answer,
                    "timeout_seconds": 2,
                },
            )
            yield StreamEnd(
                stop_reason="tool_use",
                assistant_content=[
                    {
                        "type": "tool_use",
                        "id": "message-1",
                        "name": "agent_message",
                        "input": {
                            "kind": "question",
                            "message": "Should I include generated files?",
                            "wait_for_answer": self.wait_for_answer,
                            "timeout_seconds": 2,
                        },
                    }
                ],
            )
            return
        yield StreamTextDelta("child resumed and completed")
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


class BrokenProvider(MessageProvider):
    """Fail before producing a child response."""

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
        if self.calls < 0:  # pragma: no cover
            yield StreamTextDelta("")
        raise ProviderError("child provider failed")


class SelfClaimProvider(Provider):
    """Claim the shared Todo task, complete it, then return a result."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "self-claim-test"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        del system, max_tokens, temperature, kwargs
        self.calls += 1
        if self.calls == 1:
            assert tools is not None
            assert {"todo_claim", "todo_update"}.issubset({tool["name"] for tool in tools})
            yield StreamToolUse("claim-1", "todo_claim", {})
            yield StreamEnd(
                stop_reason="tool_use",
                assistant_content=[
                    {
                        "type": "tool_use",
                        "id": "claim-1",
                        "name": "todo_claim",
                        "input": {},
                    }
                ],
            )
            return
        if self.calls == 2:
            claim: dict[str, Any] | None = None
            for message in reversed(messages):
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    raw = block.get("content")
                    if not isinstance(raw, str):
                        continue
                    with_context = json.loads(raw)
                    if with_context.get("status") == "claimed":
                        claim = with_context["claim"]
                        break
                if claim is not None:
                    break
            assert claim is not None
            item = claim["item"]
            arguments = {
                "item_id": item["id"],
                "status": "completed",
                "expected_version": claim["plan_version"],
                "expected_revision": item["revision"],
            }
            yield StreamToolUse("update-1", "todo_update", arguments)
            yield StreamEnd(
                stop_reason="tool_use",
                assistant_content=[
                    {
                        "type": "tool_use",
                        "id": "update-1",
                        "name": "todo_update",
                        "input": arguments,
                    }
                ],
            )
            return
        yield StreamTextDelta("claimed and completed the shared task")
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


def make_parent(tmp_path: Path, provider: Provider) -> Agent:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Carbide Tests"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "carbide-tests@example.invalid"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return Agent(
        provider=provider,
        project_dir=tmp_path,
        tool_registry=create_default_registry(),
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
    )


def test_agent_tool_creates_generic_workers_without_role_parameters(tmp_path: Path) -> None:
    agent = make_parent(tmp_path, MessageProvider())
    assert agent.subagent_manager is not None
    schema = AgentSpawnTool(agent.subagent_manager).api_schema()
    properties = schema["input_schema"]["properties"]
    assert "mode" not in properties
    assert "role" not in properties
    assert "task" in properties


@pytest.mark.asyncio
async def test_worker_cannot_mark_parent_context_for_snipping(tmp_path: Path) -> None:
    from carbide.context.snip import SnipCompressor
    from carbide.tools.snip import SnipTool

    agent = make_parent(tmp_path, MessageProvider())
    assert agent.loop.tool_registry is not None
    assert agent.todo_service is not None
    assert agent.subagent_manager is not None
    agent.loop.tool_registry.register(SnipTool(SnipCompressor()))
    child = agent.subagent_manager._child_registry("worker", todo_service=agent.todo_service)
    assert "snip" not in child
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_generic_worker_self_claims_shared_todo_task(tmp_path: Path) -> None:
    provider = SelfClaimProvider()
    agent = make_parent(tmp_path, provider)
    assert agent.todo_service is not None
    agent.todo_service.initialize()
    agent.todo_service.add("Worker-owned task", expected_version=0)
    assert agent.subagent_manager is not None

    result = await agent.subagent_manager.dispatch(
        SubagentRequest(task="Work from the shared Todo plan")
    )

    assert result.status.value == "completed"
    snapshot = agent.todo_service.snapshot()
    assert snapshot is not None
    assert snapshot.items[0].status.value == "completed"
    assert snapshot.items[0].owner_id is None
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_child_question_is_durable_and_scoped_to_parent(tmp_path: Path) -> None:
    agent = make_parent(tmp_path, MessageProvider())
    assert agent.subagent_manager is not None

    result = await agent.subagent_manager.dispatch(
        SubagentRequest(task="inspect the generated files")
    )

    assert result.status.value == "completed"
    messages = agent.subagent_manager.parent_messages()
    assert len(messages) == 1
    assert messages[0]["kind"] == "question"
    assert messages[0]["content"] == "Should I include generated files?"
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_background_question_waits_for_parent_and_result_is_reclaimed(
    tmp_path: Path,
) -> None:
    agent = make_parent(tmp_path, MessageProvider(wait_for_answer=True))
    assert agent.subagent_manager is not None
    manager = agent.subagent_manager

    running = await manager.dispatch(
        SubagentRequest(task="wait for an inclusion decision", background=True)
    )
    assert running.status.value == "running"
    assert running.agent_id

    for _ in range(100):
        status = manager.status(running.agent_id)
        if status and status["status"] == "waiting_for_parent":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("background child did not enter waiting_for_parent")

    manager.send(
        running.agent_id,
        kind="answer",
        content="Include only source files.",
    )
    for _ in range(100):
        updates = manager.poll_completed()
        if updates:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("background child did not publish a completed result")

    assert updates[0]["status"] == "completed"
    assert "child resumed" in updates[0]["result"]["final_text"]
    assert manager.stats()["background"] == 0
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_background_failure_is_published_to_parent_mailbox(tmp_path: Path) -> None:
    agent = make_parent(tmp_path, BrokenProvider())
    assert agent.subagent_manager is not None
    manager = agent.subagent_manager

    running = await manager.dispatch(SubagentRequest(task="fail", background=True))
    assert running.agent_id
    for _ in range(100):
        updates = manager.poll_completed()
        if updates:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("background failure did not publish a result")

    assert updates[0]["status"] == "failed"
    assert updates[0]["error"]
    mailbox = manager.parent_messages()
    assert mailbox[0]["kind"] == "result"
    assert mailbox[0]["content"]["status"] == "failed"
    await agent.__aexit__(None, None, None)


def test_parent_mailbox_claim_is_scoped_and_acknowledged(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.open()
    store.create_session("parent-a", tmp_path)
    store.create_session("parent-b", tmp_path)
    store.create_agent_run(
        run_id="run-a",
        parent_session_id="parent-a",
        child_session_id=None,
        mode="regular",
        role="explore",
        task="a",
        background=True,
    )
    store.create_agent_run(
        run_id="run-b",
        parent_session_id="parent-b",
        child_session_id=None,
        mode="regular",
        role="explore",
        task="b",
        background=True,
    )
    store.append_agent_message(
        run_id="run-a",
        direction="child_to_parent",
        kind="progress",
        content="a-progress",
    )
    store.append_agent_message(
        run_id="run-b",
        direction="child_to_parent",
        kind="progress",
        content="b-progress",
    )

    messages = store.claim_agent_messages_for_parent(parent_session_id="parent-a")
    assert [message["content"] for message in messages] == ["a-progress"]
    store.acknowledge_agent_message(
        messages[0]["id"],
        delivery_token=messages[0]["delivery_token"],
    )
    assert store.claim_agent_messages_for_parent(parent_session_id="parent-a") == []
    assert store.claim_agent_messages_for_parent(parent_session_id="parent-b")[0]["content"] == (
        "b-progress"
    )
    store.close()


def test_agent_lease_reaps_stale_owner_and_fences_writes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.open()
    store.create_session("parent", tmp_path)
    store.create_agent_run(
        run_id="stale-run",
        parent_session_id="parent",
        child_session_id=None,
        mode="regular",
        role="explore",
        task="stale",
        background=True,
        owner_id="old-process",
        lease_seconds=60,
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", "stale-run"),
        )
        connection.commit()

    assert store.reap_expired_agent_runs(
        parent_session_id="parent",
        exclude_owner_id="new-process",
    ) == ["stale-run"]
    record = store.get_agent_run("stale-run")
    assert record is not None
    assert record["status"] == "orphaned"
    assert record["owner_id"] is None
    assert (
        store.update_agent_run(
            "stale-run",
            status="completed",
            owner_id="old-process",
        )
        is False
    )
    with pytest.raises(ValueError, match="lease is no longer owned"):
        store.append_agent_message(
            run_id="stale-run",
            direction="child_to_parent",
            kind="result",
            content="late result",
            owner_id="old-process",
        )
    store.close()


def test_agent_heartbeat_preserves_live_owner(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.open()
    store.create_session("parent", tmp_path)
    store.create_agent_run(
        run_id="live-run",
        parent_session_id="parent",
        child_session_id=None,
        mode="regular",
        role="explore",
        task="live",
        background=True,
        owner_id="live-process",
        lease_seconds=60,
    )

    assert (
        store.reap_expired_agent_runs(
            parent_session_id="parent",
            exclude_owner_id="live-process",
        )
        == []
    )
    assert (
        store.heartbeat_agent_runs(
            parent_session_id="parent",
            owner_id="live-process",
            lease_seconds=60,
        )
        == 1
    )
    record = store.get_agent_run("live-run")
    assert record is not None
    assert record["status"] == "queued"
    assert record["owner_id"] == "live-process"
    assert record["lease_until"]
    store.close()
