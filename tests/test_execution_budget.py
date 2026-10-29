"""Planning-budget notices must not change completion, pairing, or default limits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from carbide.engine.budget import ExecutionBudget
from carbide.engine.loop import QueryLoop
from carbide.engine.repair import verify_pairing
from carbide.governance import SessionStore
from carbide.providers.base import StreamEnd, StreamTextDelta, StreamToolUse
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry
from tests.conftest import ScriptedProvider


def test_unbounded_execution_adds_no_notices() -> None:
    budget = ExecutionBudget()
    assert all(budget.notice(calls_used=index, now=index * 100) is None for index in range(100))


def test_call_notices_are_bounded_and_include_upcoming_call() -> None:
    budget = ExecutionBudget(max_calls=40)
    assert budget.notice(calls_used=34, now=0) is None
    notice = budget.notice(calls_used=35, now=0)
    assert notice is not None and notice["remaining_calls"] == 5
    assert "including the upcoming call" in notice["message"]
    assert "do not claim success" in notice["message"]
    assert all(budget.notice(calls_used=index, now=0) is None for index in (36, 37, 38))
    final = budget.notice(calls_used=39, now=0)
    assert final is not None and final["remaining_calls"] == 1
    assert budget.notice(calls_used=39, now=0) is None


def test_time_notices_are_bounded_and_do_not_extend_deadline() -> None:
    budget = ExecutionBudget(deadline=100)
    assert budget.notice(calls_used=0, now=39) is None
    first = budget.notice(calls_used=1, now=40)
    assert first is not None and first["remaining_seconds"] == 60
    assert budget.notice(calls_used=2, now=75) is None
    final = budget.notice(calls_used=3, now=85)
    assert final is not None and final["remaining_seconds"] == 15
    assert budget.notice(calls_used=4, now=101) is None
    assert budget.deadline == 100


def test_simultaneous_threshold_crossings_coalesce_into_one_notice() -> None:
    budget = ExecutionBudget(max_calls=1, deadline=10)
    notice = budget.notice(calls_used=0, now=0)
    assert notice is not None
    assert notice["remaining_calls"] == 1 and notice["remaining_seconds"] == 10
    assert budget.notice(calls_used=0, now=1) is None


@pytest.mark.parametrize("deadline", [float("inf"), float("-inf"), float("nan")])
def test_invalid_clock_values_are_rejected(deadline: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        ExecutionBudget(deadline=deadline)


def read_turn(index: int) -> list[StreamToolUse | StreamEnd]:
    call = {"type": "tool_use", "id": str(index), "name": "read", "input": {"path": "file.txt"}}
    return [
        StreamToolUse(str(index), "read", {"path": "file.txt"}),
        StreamEnd(stop_reason="tool_use", assistant_content=[call]),
    ]


@pytest.mark.parametrize("enabled", [True, False])
async def test_budget_notices_preserve_tool_pairing_and_do_not_fake_completion(
    tmp_path: Path,
    enabled: bool,
) -> None:
    (tmp_path / "file.txt").write_text("content", encoding="utf-8")
    provider = ScriptedProvider([read_turn(index) for index in range(8)])
    registry = create_default_registry()
    loop = QueryLoop(
        provider=provider,
        workspace=tmp_path,
        tool_registry=registry,
        max_turns=8,
        enable_budget_notices=enabled,
    )
    events = [
        event
        async for event in loop.query("read", system_prompt="stable", tools=registry.api_schemas())
    ]
    assert provider.call_count == 8
    assert events[-1].data["reason"] == "max_turns"
    assert len([event for event in events if event.type == "budget_warning"]) == (
        2 if enabled else 0
    )
    assert verify_pairing(loop.state.messages) == []
    assert all(call["system"] == provider.calls[0]["system"] for call in provider.calls)
    assert sum(event.type == "tool_complete" for event in events) == 8


async def test_default_query_does_not_add_a_limit_or_notice(tmp_path: Path) -> None:
    provider = ScriptedProvider([[StreamEnd()]])
    loop = QueryLoop(provider=provider, workspace=tmp_path)
    events = [event async for event in loop.query("hello")]
    assert loop.max_turns is None
    assert not any(event.type == "budget_warning" for event in events)
    assert events[-1].data["reason"] == "completed"


async def test_sdk_passes_deadline_and_persists_notice_for_resume(tmp_path: Path) -> None:
    provider = ScriptedProvider([[StreamTextDelta("done"), StreamEnd()]])
    async with Agent(
        provider=provider,
        project_dir=tmp_path,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
        enable_governance=False,
        enable_subagents=False,
    ) as agent:
        events = [
            event
            async for event in agent.query(
                "finish", deadline=asyncio.get_running_loop().time() + 10
            )
        ]
        assert len([event for event in events if event.type == "budget_warning"]) == 1
        assert events[-1].data["reason"] == "completed"
        assert agent.session_store is not None and agent.session_id is not None
        persisted = agent.session_store.load_messages(agent.session_id)
        assert sum("<execution-budget>" in str(message) for message in persisted) == 1


async def test_notice_failure_is_visible_and_prevents_unpersisted_model_context(
    tmp_path: Path,
) -> None:
    with SessionStore(tmp_path / "store.db") as store:
        store.create_session("session", tmp_path)
        with store.transaction() as connection:
            connection.execute("""CREATE TRIGGER reject_budget BEFORE INSERT ON messages
                WHEN NEW.content LIKE '%<execution-budget>%'
                BEGIN SELECT RAISE(ABORT, 'budget write failed'); END""")
        provider = ScriptedProvider([])
        loop = QueryLoop(
            provider=provider,
            workspace=tmp_path,
            max_turns=1,
            session_store=store,
            session_id="session",
        )
        events = [event async for event in loop.query("finish")]
        assert provider.call_count == 0
        assert events[-1].data["reason"] == "budget_context_error"
        assert any(event.type == "error" for event in events)
        assert len(store.load_messages("session")) == 1
        with store.transaction():
            pass
