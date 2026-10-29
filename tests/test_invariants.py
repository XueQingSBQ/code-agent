"""The message-history invariant, checked with Hypothesis.

Every ``tool_use`` must have a matching ``tool_result`` or the next API request
is rejected. This is the hard invariant behind the M2 fault-tolerance number, so
it gets property-based coverage rather than a handful of examples: Hypothesis
generates histories truncated at arbitrary points and asserts repair always fixes
them.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from carbide.engine.repair import find_orphan_tool_uses, repair_messages, verify_pairing

_tool_names = st.sampled_from(["read", "write", "edit", "bash", "glob", "grep"])


@st.composite
def conversation(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Build a plausible history: user turn, then assistant turns with tools."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "do the thing"}]
    turns = draw(st.integers(min_value=1, max_value=5))
    counter = 0
    for _ in range(turns):
        tool_count = draw(st.integers(min_value=0, max_value=3))
        blocks: list[dict[str, Any]] = [{"type": "text", "text": "working"}]
        ids: list[str] = []
        for _ in range(tool_count):
            counter += 1
            tool_id = f"toolu_{counter:04d}"
            ids.append(tool_id)
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": draw(_tool_names),
                    "input": {},
                }
            )
        messages.append({"role": "assistant", "content": blocks})
        if ids:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tid, "content": "ok"} for tid in ids
                    ],
                }
            )
    return messages


@given(conversation())
@settings(max_examples=200, deadline=None)
def test_wellformed_history_needs_no_repair(messages: list[dict[str, Any]]) -> None:
    assert verify_pairing(messages) == []
    repaired, count = repair_messages(messages)
    assert count == 0
    assert repaired == messages


@given(conversation(), st.integers(min_value=1, max_value=40))
@settings(max_examples=300, deadline=None)
def test_truncated_history_is_always_repaired(messages: list[dict[str, Any]], cut: int) -> None:
    """Truncation at any point must be repairable.

    This models every real interruption: Ctrl+C, dropped connection, killed
    process. Whatever survives on disk must be turned back into a valid request.
    """
    truncated = messages[: max(1, cut % (len(messages) + 1))]
    repaired, count = repair_messages(truncated)
    assert verify_pairing(repaired) == [], "repair left an unpaired tool_use"
    assert count == len(find_orphan_tool_uses(truncated))
    # Repair only ever adds; it must never drop history.
    assert len(repaired) >= len(truncated)


@given(conversation(), st.integers(min_value=1, max_value=40))
@settings(max_examples=100, deadline=None)
def test_repair_is_idempotent(messages: list[dict[str, Any]], cut: int) -> None:
    truncated = messages[: max(1, cut % (len(messages) + 1))]
    once, _ = repair_messages(truncated)
    twice, second_count = repair_messages(once)
    assert second_count == 0
    assert twice == once


def test_partial_tool_results_are_merged_not_duplicated() -> None:
    """Two tools dispatched, only one answered: the synthetic result joins the
    existing tool_result message rather than creating a second one."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
                {"type": "tool_use", "id": "toolu_2", "name": "read", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}],
        },
    ]
    repaired, count = repair_messages(messages)
    assert count == 1
    assert verify_pairing(repaired) == []
    assert len(repaired) == 3, "should merge, not append a fourth message"
    result_ids = [b["tool_use_id"] for b in repaired[2]["content"]]
    assert result_ids == ["toolu_1", "toolu_2"]
