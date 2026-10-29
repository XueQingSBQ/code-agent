"""Boundary and security regression tests for context compression layers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from carbide.context.restore import RestoreCompressor
from carbide.context.snip import SnipCompressor, SnipStrategy
from carbide.context.spill import SpillCompressor


@pytest.mark.asyncio
async def test_restore_skips_non_compacted_missing_and_outside_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-carbide.txt"
    outside.write_text("must not be read", encoding="utf-8")
    try:
        compressor = RestoreCompressor(tmp_path)
        ordinary = [{"role": "user", "content": "ordinary history"}]
        restored, result = await compressor.restore(ordinary, [outside])
        assert restored == ordinary
        assert not result.restored

        compacted = [{"role": "assistant", "content": "summary", "_l4_summary": True}]
        restored, result = await compressor.restore(
            compacted,
            [Path("missing.txt"), Path("..") / outside.name],
        )
        assert restored == compacted
        assert result.restored
        assert result.files_restored == 0
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_restore_truncates_and_restores_skills_and_plan(tmp_path: Path) -> None:
    file_path = tmp_path / "large.py"
    file_path.write_text("x" * 100, encoding="utf-8")
    compressor = RestoreCompressor(
        tmp_path, max_files=1, max_tokens_per_file=5, max_total_tokens=10_000
    )

    restored, result = await compressor.restore(
        [{"role": "assistant", "content": "summary", "_l4_summary": True}],
        [file_path],
        active_skills=["python"],
        current_plan="run tests",
    )

    assert result.files_restored == 1
    assert result.skills_restored == 1
    assert len(restored) == 4
    assert "truncated" in str(restored[1]).lower()
    assert restored[-1]["_l45_restored_plan"] is True


def test_restore_extracts_only_valid_workspace_paths(tmp_path: Path) -> None:
    valid = tmp_path / "src" / "main.py"
    valid.parent.mkdir()
    valid.write_text("pass", encoding="utf-8")
    compressor = RestoreCompressor(tmp_path)
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": [{"type": "tool_use", "input": "bad"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "input": {"path": "src/main.py"}},
                {"type": "tool_use", "input": {"path": "../outside.py"}},
            ],
        },
    ]

    assert compressor.extract_recently_accessed_files(messages) == [valid.resolve()]
    assert compressor.extract_recently_accessed_files(messages, lookback=0) == []


def test_restore_rejects_invalid_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_files"):
        RestoreCompressor(tmp_path, max_files=-1)
    with pytest.raises(ValueError, match="max_tokens_per_file"):
        RestoreCompressor(tmp_path, max_tokens_per_file=0)
    with pytest.raises(ValueError, match="max_total_tokens"):
        RestoreCompressor(tmp_path, max_total_tokens=0)


def test_spill_uses_message_total_threshold_and_safe_filename(tmp_path: Path) -> None:
    compressor = SpillCompressor(
        tmp_path / "spill",
        single_result_threshold=100,
        message_total_threshold=150,
        preview_size=10,
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "..\\escape/one", "content": "a" * 90},
                {"type": "tool_result", "tool_use_id": "two", "content": "b" * 90},
            ],
        }
    ]

    compressed, result = compressor.compress(messages)
    assert result.spilled
    assert sum(block.get("_spilled", False) for block in compressed[0]["content"]) == 1
    assert result.spill_path is not None
    assert result.spill_path.parent == (tmp_path / "spill")
    assert ".." not in result.spill_path.name
    assert json.loads(result.spill_path.read_text(encoding="utf-8"))["content"]


def test_spill_restore_cleanup_and_stats(tmp_path: Path) -> None:
    spill_dir = tmp_path / "spill"
    compressor = SpillCompressor(spill_dir, single_result_threshold=10, preview_size=4)
    compressed, result = compressor.compress(
        [{"role": "user", "content": [{"type": "tool_result", "content": "x" * 20}]}]
    )
    assert result.spill_path is not None
    assert compressor.restore_from_disk(result.spill_path) is not None
    assert compressor.restore_from_disk(spill_dir / "missing.json") is None
    corrupted = spill_dir / "corrupt.json"
    corrupted.write_text("not json", encoding="utf-8")
    assert compressor.restore_from_disk(corrupted) is None
    os.utime(corrupted, (time.time() - 10, time.time() - 10))
    assert compressor.cleanup_spilled_files(older_than_seconds=1) == 1
    assert compressor.get_stats()["spill_count"] == 1
    assert compressed[0]["content"][0]["_spilled"] is True


def test_snip_removes_messages_and_individual_blocks() -> None:
    compressor = SnipCompressor()
    messages: list[dict[str, Any]] = [
        {"id": "whole", "role": "user", "content": "remove"},
        {
            "id": "partial",
            "role": "user",
            "content": [
                {"type": "text", "text": "keep"},
                {"type": "tool_result", "tool_use_id": "remove-block", "content": "old"},
            ],
        },
        {"_id": "empty", "role": "assistant", "content": [{"id": "remove-block-2"}]},
    ]
    compressor.mark_multiple(["whole", "remove-block", "remove-block-2", "not-found"])

    compressed, result = compressor.compress(messages)
    assert result.messages_removed == 2
    assert len(compressed) == 1
    assert compressed[0]["content"] == [{"type": "text", "text": "keep"}]
    assert not compressor.has_pending_snips()
    compressor.clear_snip_marks()
    assert not compressor.has_pending_snips()


def test_snip_strategies_suggest_old_results_and_redundant_reads() -> None:
    messages: list[dict[str, Any]] = [
        {"id": "one", "role": "user", "content": [{"type": "tool_result"}]},
        {"id": "two", "role": "user", "content": [{"type": "tool_result"}]},
        {"id": "three", "role": "user", "content": [{"type": "tool_result"}]},
    ]
    assert SnipStrategy.suggest_old_tool_results(messages, keep_recent=1) == ["one", "two"]

    reads: list[dict[str, Any]] = [
        {
            "id": "r1",
            "role": "user",
            "content": [{"type": "tool_result", "content": "file_path=a.py"}],
        },
        {
            "id": "r2",
            "role": "user",
            "content": [{"type": "tool_result", "content": "file_path=a.py"}],
        },
    ]
    assert SnipStrategy.suggest_redundant_reads(reads) == ["r1"]


def test_compression_layers_validate_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single_result_threshold"):
        SpillCompressor(tmp_path / "spill", single_result_threshold=0)
    with pytest.raises(ValueError, match="message_total_threshold"):
        SpillCompressor(tmp_path / "spill", message_total_threshold=0)
    with pytest.raises(ValueError, match="preview_size"):
        SpillCompressor(tmp_path / "spill", preview_size=0)
    with pytest.raises(ValueError, match="older_than_seconds"):
        SpillCompressor(tmp_path / "spill").cleanup_spilled_files(-1)
