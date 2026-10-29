"""Controlled live endpoint matrix.

Run explicitly with ``uv run --env-file .env pytest -m live -s``. These tests
are intentionally excluded from the offline release gate because they spend
provider quota and require an Anthropic-compatible endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carbide.infra.config import Config
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry

pytestmark = pytest.mark.live


async def _allow_tool(*_: Any) -> bool:
    return True


async def _query(
    agent: Agent,
    prompt: str,
    tools: list[dict[str, Any]],
) -> list[Any]:
    events: list[Any] = []
    async for event in agent.query(prompt, tools=tools):
        events.append(event)
    return events


def _text(events: list[Any]) -> str:
    chunks: list[str] = []
    for event in events:
        if event.type != "chunk" or event.data is None:
            continue
        content = getattr(event.data, "content", None)
        if isinstance(content, str):
            chunks.append(content)
    return "".join(chunks)


def _tool_events(events: list[Any]) -> list[dict[str, Any]]:
    return [
        event.data
        for event in events
        if event.type == "tool_complete" and isinstance(event.data, dict)
    ]


def _turn_usage(events: list[Any]) -> dict[str, int]:
    completed = [
        event.data
        for event in events
        if event.type == "turn_complete" and isinstance(event.data, dict)
    ]
    return completed[-1] if completed else {}


@pytest.mark.asyncio
async def test_live_basic_stream_and_usage(tmp_path: Path) -> None:
    config = Config.load(workspace=tmp_path)
    registry = create_default_registry()
    async with Agent(
        config=config,
        project_dir=tmp_path,
        tool_registry=registry,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
        approval_callback=_allow_tool,
    ) as agent:
        events = await _query(agent, "Reply with exactly LIVE_BASIC_OK", registry.api_schemas())

    response = _text(events)
    usage = _turn_usage(events)
    assert response.strip(), "live endpoint returned no streamed text"
    assert usage.get("input_tokens", 0) > 0
    assert usage.get("output_tokens", 0) > 0
    print(
        f"live_basic response_chars={len(response)} "
        f"input={usage.get('input_tokens', 0)} "
        f"output={usage.get('output_tokens', 0)} "
        f"cache_read={usage.get('cache_read_tokens', 0)}"
    )


@pytest.mark.asyncio
async def test_live_multiturn_tool_flow_and_usage(tmp_path: Path) -> None:
    config = Config.load(workspace=tmp_path)
    registry = create_default_registry()
    async with Agent(
        config=config,
        project_dir=tmp_path,
        tool_registry=registry,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
        approval_callback=_allow_tool,
    ) as agent:
        tools = registry.api_schemas()
        first = await _query(
            agent,
            "Use the write tool now. Create live_probe.txt with exactly "
            "LIVE_API_TOOL_OK. Do not just explain; call the tool.",
            tools,
        )
        second = await _query(
            agent,
            "Use the read tool now to read live_probe.txt, then reply with its exact contents.",
            tools,
        )

    first_tools = _tool_events(first)
    second_tools = _tool_events(second)
    assert any(
        item.get("tool_name") == "write" and not item.get("is_error") for item in first_tools
    )
    assert any(
        item.get("tool_name") == "read" and not item.get("is_error") for item in second_tools
    )
    assert "LIVE_API_TOOL_OK" in _text(second)
    first_usage = _turn_usage(first)
    second_usage = _turn_usage(second)
    assert first_usage.get("input_tokens", 0) > 0
    assert second_usage.get("input_tokens", 0) > 0
    print(
        "live_multiturn "
        f"first_in={first_usage.get('input_tokens', 0)} "
        f"second_in={second_usage.get('input_tokens', 0)} "
        f"second_cache_read={second_usage.get('cache_read_tokens', 0)}"
    )
