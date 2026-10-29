"""Runtime coverage for L4/L4.5 and workspace-safe file tracking."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carbide.context.budget import TokenBudget
from carbide.context.manager import CompressionManager
from carbide.engine.loop import QueryLoop


@pytest.mark.asyncio
async def test_l4_compaction_restores_relative_workspace_file(tmp_path: Path) -> None:
    active_file = tmp_path / "active.py"
    active_file.write_text("print('active')\n", encoding="utf-8")
    manager = CompressionManager(
        workspace=tmp_path,
        budget=TokenBudget(max_tokens=1_000, compression_threshold=0.8),
    )
    messages = [
        {"role": "user", "content": f"history {index} " + ("x" * 1_000)} for index in range(8)
    ]

    async def summarize(history: list[dict[str, Any]], instruction: str) -> str:
        del history, instruction
        return "1. Task Objective\nContinue work\n9. Next Steps\nInspect active.py"

    compressed, result = await manager.compress(
        messages,
        summarize_func=summarize,
        recently_accessed_files=[Path("active.py")],
    )

    assert result.failure is None
    assert result.l4_result is not None and result.l4_result.compacted
    assert result.l45_result is not None and result.l45_result.files_restored == 1
    assert "L4:compact" in result.layers_triggered
    assert "L4.5:restore" in result.layers_triggered
    assert any(message.get("_l45_restored_file") == str(active_file) for message in compressed)


@pytest.mark.asyncio
async def test_l4_failure_is_reported_to_caller(tmp_path: Path) -> None:
    manager = CompressionManager(
        workspace=tmp_path,
        budget=TokenBudget(max_tokens=1_000, compression_threshold=0.8),
    )
    messages = [{"role": "user", "content": "x" * 1_000} for _ in range(8)]

    async def broken_summary(history: list[dict[str, Any]], instruction: str) -> str:
        del history, instruction
        raise RuntimeError("summary provider unavailable")

    compressed, result = await manager.compress(messages, summarize_func=broken_summary)

    assert compressed == messages
    assert result.failure == "L4 compaction failed"


def test_file_tracking_is_workspace_relative_and_bounded(tmp_path: Path) -> None:
    inside = tmp_path / "src" / "main.py"
    inside.parent.mkdir()
    inside.write_text("pass\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    try:
        loop = QueryLoop(provider=object(), workspace=tmp_path)  # type: ignore[arg-type]
        loop._track_file_access("src/main.py")
        loop._track_file_access(str(outside))

        assert loop.state.active_files == [str(inside.resolve())]
    finally:
        outside.unlink(missing_ok=True)
