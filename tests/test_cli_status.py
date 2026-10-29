"""CLI regression tests for streaming output and terminal status propagation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from rich.console import Console

from carbide.cli.app import _approval_prompt, run_once
from carbide.cli.render import Renderer
from carbide.engine.loop import QueryEvent
from carbide.providers.base import StreamChunk, StreamToolUse


def test_approval_prompt_shows_bash_command() -> None:
    prompt = _approval_prompt(
        StreamToolUse(id="bash-1", name="bash", arguments={"command": "git status\n--short"}),
        "Tool 'bash' requires approval: destructive operation. Allow?",
    )

    assert "[permission] bash" in prompt
    assert "command: git status --short" in prompt
    assert prompt.endswith("Allow? [y/N] ")


class ScriptedAgent:
    def __init__(self, events: list[QueryEvent]) -> None:
        self.events = events

    async def query(
        self, message: str, tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[QueryEvent]:
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_run_once_preserves_non_success_terminal_reason() -> None:
    console = Console(record=True, width=120)
    renderer = Renderer(console)
    agent = ScriptedAgent(
        [
            QueryEvent(
                type="chunk",
                data=StreamChunk(type="content_block_delta", content="partial"),
            ),
            QueryEvent(
                type="turn_complete",
                data={"turn": 1, "reason": "max_turns"},
            ),
        ]
    )

    await run_once(agent, renderer, "continue")  # type: ignore[arg-type]

    assert "[max_turns]" in console.export_text()


@pytest.mark.asyncio
async def test_run_once_renders_error_reason_from_engine() -> None:
    console = Console(record=True, width=120)
    renderer = Renderer(console)
    agent = ScriptedAgent(
        [QueryEvent(type="error", data={"reason": "compaction_failed", "message": "boom"})]
    )

    await run_once(agent, renderer, "continue")  # type: ignore[arg-type]

    assert "[compaction_failed]" in console.export_text()


@pytest.mark.asyncio
async def test_run_once_renders_progress_events_before_footer() -> None:
    console = Console(record=True, width=120)
    renderer = Renderer(console)
    agent = ScriptedAgent(
        [
            QueryEvent(type="turn_started", data={"turn": 1}),
            QueryEvent(
                type="chunk",
                data=StreamChunk(type="content_block_delta", content="checking"),
            ),
            QueryEvent(
                type="tool_use",
                data={"id": "tool-1", "name": "read", "input": {"path": "main.py"}},
            ),
            QueryEvent(
                type="tool_complete",
                data={"tool_name": "read", "duration_ms": 12.5, "is_error": False},
            ),
            QueryEvent(
                type="turn_complete",
                data={"turn": 1, "reason": "completed"},
            ),
        ]
    )

    await run_once(agent, renderer, "inspect")  # type: ignore[arg-type]

    output = console.export_text()
    assert output.index("turn 1 started") < output.index("read")
    assert output.index("read") < output.index("[completed]")
    assert "checking" in output


@pytest.mark.asyncio
async def test_run_once_resets_usage_between_queries() -> None:
    console = Console(record=True, width=120)
    renderer = Renderer(console)

    first = ScriptedAgent(
        [
            QueryEvent(
                type="turn_complete",
                data={
                    "turn": 1,
                    "reason": "completed",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_tokens": 40,
                },
            )
        ]
    )
    second = ScriptedAgent(
        [
            QueryEvent(
                type="turn_complete",
                data={
                    "turn": 1,
                    "reason": "completed",
                    "input_tokens": 200,
                    "output_tokens": 20,
                    "cache_read_tokens": 80,
                },
            )
        ]
    )

    await run_once(first, renderer, "first")  # type: ignore[arg-type]
    await run_once(second, renderer, "second")  # type: ignore[arg-type]

    footers = [line for line in console.export_text().splitlines() if "[completed]" in line]
    assert len(footers) == 2
    assert "in 280" in footers[-1]
    assert "out 20" in footers[-1]
    assert "in 420" not in footers[-1]
    assert "out 30" not in footers[-1]
