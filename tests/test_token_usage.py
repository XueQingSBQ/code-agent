"""Regression tests for token accounting and prompt-cache telemetry."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from carbide.cli.render import Renderer
from carbide.engine.loop import QueryLoop
from carbide.events import Done, ExitReason, TurnCompleted
from carbide.providers.anthropic import AnthropicProvider
from carbide.providers.base import StreamEnd, Usage
from tests.conftest import ScriptedProvider


async def test_stream_end_usage_is_counted_once(tmp_path: Path) -> None:
    """The typed terminal event must not also enter legacy usage accounting."""

    provider = ScriptedProvider(
        [
            [
                StreamEnd(
                    usage=Usage(
                        input_tokens=37,
                        output_tokens=11,
                        cache_read_tokens=13,
                        cache_creation_tokens=17,
                    ),
                    assistant_content=[{"type": "text", "text": "done"}],
                )
            ]
        ]
    )
    loop = QueryLoop(provider=provider, workspace=tmp_path, max_turns=1)

    events = [event async for event in loop.query("hello")]
    completed = next(event for event in events if event.type == "turn_complete")

    assert completed.data["input_tokens"] == 37
    assert completed.data["output_tokens"] == 11
    assert completed.data["cache_read_tokens"] == 13
    assert completed.data["cache_creation_tokens"] == 17
    assert loop.state.total_input_tokens == 37
    assert loop.state.total_output_tokens == 11
    assert loop.state.total_cache_read_tokens == 13
    assert loop.state.total_cache_creation_tokens == 17


def test_renderer_uses_all_input_buckets_for_hit_rate() -> None:
    console = Console(record=True, width=120)
    renderer = Renderer(console)

    renderer.handle(
        TurnCompleted(
            turn=1,
            input_tokens=10,
            output_tokens=2,
            cache_read_tokens=20,
            cache_creation_tokens=30,
        )
    )
    renderer.handle(Done(reason=ExitReason.COMPLETED, turns=1))

    # ``in`` is the complete prompt; the provider buckets remain visible.
    output = console.export_text()
    assert "in 60 (uncached 10, read 20, write 30)" in output
    assert "hit 33%" in output


def test_anthropic_provider_adds_cache_breakpoints_without_mutation() -> None:
    system = [{"type": "text", "text": "stable system"}]
    tools = [
        {
            "name": "read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]

    prepared_system = AnthropicProvider._prepare_system(system)
    prepared_tools = AnthropicProvider._prepare_tools(tools)

    assert isinstance(prepared_system, list)
    assert prepared_system[0]["cache_control"] == {"type": "ephemeral"}
    assert prepared_tools[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[0]
    assert "cache_control" not in tools[0]


async def test_query_loop_emits_cacheable_system_prefix(tmp_path: Path) -> None:
    provider = ScriptedProvider([[StreamEnd()]])
    loop = QueryLoop(provider=provider, workspace=tmp_path, max_turns=1)

    system = await loop._build_system_prompt("stable system", "hello")

    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_provider_keeps_cache_usage_from_compatible_message_delta() -> None:
    """Some gateways put the full usage object on message_delta."""

    provider = AnthropicProvider(api_key="test-key")
    state = provider._new_stream_state()
    event = SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason="end_turn"),
        usage=SimpleNamespace(
            input_tokens=137,
            output_tokens=41,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=30,
        ),
    )

    provider._parse_event(event, state)

    assert state["input_tokens"] == 137
    assert state["output_tokens"] == 41
    assert state["cache_read_tokens"] == 900
    assert state["cache_creation_tokens"] == 30
