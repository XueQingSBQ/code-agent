"""Regression tests for Agent startup, resume repair, and shutdown ownership."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from carbide.engine.repair import verify_pairing
from carbide.governance import SessionStore
from carbide.providers.base import (
    Provider,
    ProviderError,
    StreamEnd,
    StreamTextDelta,
    StreamToolUse,
    Usage,
)
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


def _sleep_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class FinalAnswerProvider(Provider):
    """Provider double that returns one deterministic final answer."""

    @property
    def model(self) -> str:
        return "test"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        yield StreamTextDelta("ok")
        yield StreamEnd()

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return 1

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class ApprovalBashProvider(FinalAnswerProvider):
    """Return a destructive bash call once, then a final response."""

    def __init__(self) -> None:
        self.call_count = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        self.call_count += 1
        if self.call_count == 1:
            call = StreamToolUse("tool-bash", "bash", {"command": "printf approved"})
            yield call
            yield StreamEnd(
                stop_reason="tool_use",
                usage=Usage(input_tokens=1, output_tokens=1),
                assistant_content=[
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                ],
            )
            return
        yield StreamTextDelta("continued")
        yield StreamEnd(usage=Usage(input_tokens=1, output_tokens=1))


class FailingProvider(FinalAnswerProvider):
    """Provider double that reports a terminal upstream failure."""

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
        raise ProviderError("upstream unavailable")
        yield  # pragma: no cover


class InterruptedToolProvider(FinalAnswerProvider):
    """Emit a tool call, then fail before the response reaches StreamEnd."""

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
        yield StreamToolUse("tool-interrupted", "read", {"path": "hello.py"})
        raise ProviderError("connection reset after tool dispatch")


def make_agent(tmp_path: Path, **kwargs: Any) -> Agent:
    defaults = {
        "provider": FinalAnswerProvider(),
        "project_dir": tmp_path,
        "enable_memory": False,
        "enable_skills": False,
        "enable_rules": False,
        "enable_compression": False,
    }
    defaults.update(kwargs)
    return Agent(**defaults)


@pytest.mark.asyncio
async def test_agent_uses_one_session_id_for_store_and_audit(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    assert agent.session_id is not None
    assert agent.session_store is not None
    assert agent.audit_logger is not None

    assert agent.session_store.get_session_info(agent.session_id)["session_id"] == agent.session_id
    audit_path = tmp_path / ".carbide" / "audit" / f"{agent.session_id}.jsonl"
    assert audit_path.exists()

    await agent.__aexit__(None, None, None)


def test_malformed_optional_hooks_do_not_disable_core_governance(tmp_path: Path) -> None:
    carbide_dir = tmp_path / ".carbide"
    carbide_dir.mkdir()
    (carbide_dir / "hooks").write_text("this is not a directory", encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="Project hooks disabled"):
        agent = make_agent(tmp_path)

    assert agent.hook_registry is None
    assert agent.permission_engine is not None
    assert agent.audit_logger is not None
    assert agent.bash_guard is not None
    agent.audit_logger.close()
    assert agent.session_store is not None
    agent.session_store.close()


def test_resume_repairs_and_persists_orphan_tool_use(tmp_path: Path) -> None:
    session_id = "resume-repair"
    db_path = tmp_path / ".carbide" / "sessions.db"
    with SessionStore(db_path) as store:
        store.create_session(session_id, tmp_path)
        store.append_message(
            session_id,
            "user",
            [{"type": "text", "text": "run the command"}],
        )
        store.append_message(
            session_id,
            "assistant",
            [{"type": "tool_use", "id": "tool-1", "name": "bash", "input": {}}],
        )

    agent = make_agent(tmp_path, resume_session_id=session_id)
    assert verify_pairing(agent.loop.state.messages) == []
    assert any(
        block.get("tool_use_id") == "tool-1"
        for message in agent.loop.state.messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict)
    )

    assert agent.session_store is not None
    persisted = agent.session_store.load_messages(session_id)
    assert verify_pairing(persisted) == []
    assert any(
        block.get("_synthetic") is True
        for message in persisted
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict)
    )
    agent.session_store.close()


@pytest.mark.asyncio
async def test_agent_exit_does_not_cancel_foreign_tasks(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    foreign_task = asyncio.create_task(asyncio.sleep(60))
    owned_task = asyncio.create_task(asyncio.sleep(60))
    agent._track_task(owned_task)

    await agent.__aexit__(None, None, None)

    assert not foreign_task.cancelled()
    assert not foreign_task.done()
    assert owned_task.cancelled()
    foreign_task.cancel()
    await asyncio.gather(foreign_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_exit_reclaims_attached_background_job(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    process = _sleep_process()
    agent.background_job_manager.register(process.pid, "attached test process", backend="win32")

    try:
        await agent.__aexit__(None, None, None)
        process.wait(timeout=5)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_approved_bash_call_completes_and_next_turn_streams(tmp_path: Path) -> None:
    provider = ApprovalBashProvider()
    agent = make_agent(
        tmp_path,
        provider=provider,
        tool_registry=create_default_registry(),
        approval_callback=lambda call, prompt: True,
    )

    events = [
        event
        async for event in agent.query(
            "run the approved command", tools=agent.loop.tool_registry.api_schemas()
        )
    ]

    completed = [event for event in events if event.type == "tool_complete"]
    chunks = [event.data.content for event in events if event.type == "chunk" and event.data]
    assert len(completed) == 1
    assert completed[0].data["is_error"] is False
    assert "continued" in chunks
    assert provider.call_count == 2
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_provider_error_marks_session_failed(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, provider=FailingProvider())

    events = [event async for event in agent.query("trigger failure")]
    assert any(event.type == "error" for event in events)

    await agent.__aexit__(None, None, None)

    assert agent.session_store is not None
    with SessionStore(tmp_path / ".carbide" / "sessions.db") as store:
        info = store.get_session_info(agent.session_id)
    assert info is not None
    assert info["status"] == "failed"


@pytest.mark.asyncio
async def test_provider_failure_after_tool_dispatch_keeps_history_pairing(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hello')\n", encoding="utf-8")
    agent = make_agent(
        tmp_path,
        provider=InterruptedToolProvider(),
        tool_registry=create_default_registry(),
    )

    events = [
        event
        async for event in agent.query(
            "read hello.py", tools=agent.loop.tool_registry.api_schemas()
        )
    ]

    assert any(event.type == "tool_complete" for event in events)
    assert any(event.type == "error" for event in events)
    assert verify_pairing(agent.loop.state.messages) == []
    await agent.__aexit__(None, None, None)
