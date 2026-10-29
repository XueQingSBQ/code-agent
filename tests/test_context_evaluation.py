"""Compression protocol and evaluation-accounting regression tests."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from carbide.context.compact import CompactCompressor
from carbide.context.micro import MicroCompressor
from carbide.context.restore import RestoreCompressor
from carbide.context.snip import SnipCompressor
from carbide.engine.repair import validate_message_invariants
from carbide.providers.base import (
    Message,
    Provider,
    StreamEnd,
    StreamTextDelta,
    StreamToolUse,
    Usage,
)
from carbide.sdk import Agent
from carbide.tools.fs import ReadTool
from carbide.tools.registry import ToolRegistry


def exchange(index: int) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"Inspect {index}"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"read_{index}", "name": "read", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"read_{index}",
                    "content": "x" * 2_000,
                    "_tool_name": "read",
                },
            ],
            "id": f"result-{index}",
        },
        {"role": "assistant", "content": "Important decision retained"},
    ]


@pytest.mark.parametrize("marked", ["read_1", "result-1"])
def test_snip_removes_entire_exchange_but_keeps_decisions(marked: str) -> None:
    compressor = SnipCompressor()
    messages = exchange(1) + exchange(2)
    compressor.mark_for_snip(marked)
    compressed, result = compressor.compress(messages)
    assert result.snipped
    assert not validate_message_invariants(compressed)
    assert not compressor.has_pending_snips()
    assert sum(msg.get("content") == "Important decision retained" for msg in compressed) == 2


async def test_compaction_keeps_boundary_pairs_and_measures_retained_tokens() -> None:
    messages = [message for i in range(5) for message in exchange(i)]
    messages.append({"role": "user", "content": "Continue"})

    async def summarize(history: list[dict[str, Any]], instruction: str) -> str:
        return "1. Task Objective\nKeep all decisions\n9. Next Steps\nContinue"

    compressed, result = await CompactCompressor().compress(messages, summarize)
    assert result.compacted
    assert not validate_message_invariants(compressed)
    assert result.tokens_freed == (
        sum(len(str(msg)) for msg in messages) // 4 - sum(len(str(msg)) for msg in compressed) // 4
    )


async def test_compaction_retains_latest_decision_even_when_summary_omits_it() -> None:
    messages = [message for i in range(5) for message in exchange(i)]
    messages[-1]["content"] = "Critical current decision: multiplier=5, penalty=6"
    messages.extend(exchange(5)[:3])

    async def lossy_summary(history: list[dict[str, Any]], instruction: str) -> str:
        return "1. Task Objective\nContinue work; detailed decisions omitted."

    compressed, result = await CompactCompressor().compress(messages, lossy_summary)
    assert result.compacted
    assert any(
        message.get("content") == "Critical current decision: multiplier=5, penalty=6"
        for message in compressed
    )
    assert not validate_message_invariants(compressed)


def test_micro_preserves_agent_result_in_a_mixed_parallel_batch() -> None:
    compressor = MicroCompressor(keep_recent=0)
    compressor.last_call_time = time.time() - 3_601
    message = exchange(1)[2]
    message["content"].append(
        {
            "type": "tool_result",
            "tool_use_id": "agent_1",
            "_tool_name": "agent",
            "content": "Non-reproducible finding",
        }
    )
    compressed, result = compressor.compress([message])
    assert result.messages_cleared == 1
    assert compressed[0]["content"][0]["_micro_cleared"]
    assert compressed[0]["content"][1]["content"] == "Non-reproducible finding"


def test_micro_resolves_real_tool_name_from_opaque_call_id() -> None:
    compressor = MicroCompressor(keep_recent=0)
    compressor.last_call_time = time.time() - 3_601
    messages = exchange(1)
    messages[1]["content"][0]["id"] = "toolu_a3f918"
    block = messages[2]["content"][0]
    block["tool_use_id"] = "toolu_a3f918"
    del block["_tool_name"]
    compressed, result = compressor.compress(messages)
    assert result.compacted
    assert compressed[2]["content"][0]["_micro_cleared"]
    assert not validate_message_invariants(compressed)


async def test_restore_skips_current_read_but_restores_changed_file(tmp_path: Path) -> None:
    path = tmp_path / "active.py"
    path.write_text("print('current')\n", encoding="utf-8")
    messages = exchange(1)
    messages[1]["content"][0]["input"] = {"path": "active.py"}
    messages[2]["content"][0]["content"] = "     1\tprint('current')"
    messages.append({"role": "assistant", "content": "summary", "_l4_summary": True})
    compressor = RestoreCompressor(tmp_path)
    restored, result = await compressor.restore(messages, [path])
    assert restored == messages and result.files_restored == 0
    path.write_text("print('changed')\n", encoding="utf-8")
    restored, result = await compressor.restore(messages, [path])
    assert result.files_restored == 1
    assert "print('changed')" in str(restored[-1])


async def test_restore_finds_summary_before_a_complete_recent_turn_window(tmp_path: Path) -> None:
    path = tmp_path / "contract.py"
    path.write_text("CONTRACT_NONCE = 'exact-value'\n", encoding="utf-8")
    messages = [{"role": "assistant", "content": "summary", "_l4_summary": True}]
    messages.extend(exchange(1) + exchange(2))
    restored, result = await RestoreCompressor(tmp_path).restore(messages, [path])
    assert result.files_restored == 1
    assert "exact-value" in str(restored[-1])


class ToolLoopProvider(Provider):
    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return len(str(messages)) // 4

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        self.requests.append(messages.copy())
        if len(self.requests) == 1:
            call = StreamToolUse("read_1", "read", {"path": "large.txt"})
            yield call
            yield StreamEnd(
                stop_reason="tool_use",
                usage=Usage(input_tokens=10),
                assistant_content=[
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                ],
            )
        else:
            yield StreamTextDelta("done")
            yield StreamEnd(
                usage=Usage(input_tokens=10), assistant_content=[{"type": "text", "text": "done"}]
            )


async def test_sdk_wires_snip_and_compresses_inside_one_tool_loop(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("x" * 60_000, encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadTool())
    provider = ToolLoopProvider()
    async with Agent(
        provider=provider,
        project_dir=tmp_path,
        tool_registry=registry,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_governance=False,
        enable_session_store=False,
        enable_subagents=False,
    ) as agent:
        manager = agent.compression_manager
        assert manager is not None
        manager.budget.max_tokens = 18_000
        assert registry.get("snip") is not None
        events = [event async for event in agent.query("read large.txt")]
        assert any(
            event.type == "compression" and "L1:spill" in event.data["layers"] for event in events
        )
        assert manager.l3_micro.last_call_time is not None
        assert len(str(provider.requests[1])) < 5_000
        assert not validate_message_invariants(provider.requests[1])


async def test_l4_explicitly_requests_summary_instead_of_executing_old_task(tmp_path: Path) -> None:
    class SummaryProvider(ToolLoopProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            system: str | list[dict[str, Any]] | None = None,
            tools: list[dict[str, Any]] | None = None,
            max_tokens: int = 4096,
            temperature: float = 1.0,
            **kwargs: Any,
        ) -> Message:
            self.requests.append(messages)
            return Message(
                role="assistant",
                content=[
                    {
                        "type": "text",
                        "text": "1. Task Objective\nContinue\n9. Next Steps\nRun tests",
                    }
                ],
            )

    provider = SummaryProvider()
    async with Agent(
        provider=provider,
        project_dir=tmp_path,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_governance=False,
        enable_session_store=False,
        enable_subagents=False,
    ) as agent:
        assert agent.compression_manager is not None
        agent.compression_manager.budget.max_tokens = 1_000
        agent.loop.state.messages = [message for i in range(5) for message in exchange(i)]
        event = await agent.loop._compress_context()
        assert event is not None and event.type == "compression"
        assert "Do not execute those instructions" in provider.requests[0][-1]["content"]
        assert not validate_message_invariants(agent.loop.state.messages)
