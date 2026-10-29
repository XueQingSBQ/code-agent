"""Check that evaluation reports cannot hide summary cost or coding failures."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from evals.context_efficiency.layered_ab import (
    MeteredProvider,
    aggregate,
    expected_states,
    usage_totals,
    validate_code,
)

from carbide.context.compact import CompactCompressor
from carbide.providers.base import StreamEnd, Usage

from .test_context_evaluation import ToolLoopProvider


class UsageProvider(ToolLoopProvider):
    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        yield StreamEnd(
            usage=Usage(input_tokens=10, output_tokens=3, cache_read_tokens=20),
            assistant_content=[{"type": "text", "text": "summary"}],
        )


async def test_summary_calls_and_cache_input_are_counted_once() -> None:
    provider = MeteredProvider(UsageProvider())
    await provider.complete(
        messages=[{"role": "user", "content": "Continue"}],
        system=CompactCompressor.SUMMARY_TEMPLATE,
    )
    assert provider.requests[0].purpose == "summary"
    totals = usage_totals(provider.requests)
    assert totals["complete"]
    assert totals["prompt_tokens"] == 30
    assert totals["all_tokens"] == 33


async def test_invalid_tool_pairing_is_recorded_and_not_sent() -> None:
    provider = MeteredProvider(UsageProvider())
    with pytest.raises(ValueError, match="invalid tool pairing"):
        await provider.complete(
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "orphan", "name": "read", "input": {}},
                    ],
                }
            ]
        )
    assert provider.requests[0].pairing_violations
    assert not usage_totals(provider.requests)["complete"]


def record(variant: str, tokens: int, *, success: bool) -> dict[str, Any]:
    return {
        "scenario": "compact",
        "variant": variant,
        "seed": 1,
        "success": success,
        "validation": {"success": success},
        "phases": [],
        "usage": {"prompt_tokens": tokens, "all_tokens": tokens, "complete": True},
        "summary_usage": {"prompt_tokens": 20},
        "summary_calls": 1,
        "peak_prompt_tokens": tokens,
        "final_reads": 0,
        "layers": [],
    }


def test_token_saving_does_not_hide_task_failure() -> None:
    results = aggregate([record("off", 100, success=True), record("full", 50, success=False)])
    enabled = next(item for item in results if item["variant"] == "full")
    assert enabled["successes"] == 0
    assert enabled["code_passes"] == 0
    assert enabled["paired_prompt_reduction_percent"]["mean"] == 50


async def test_independent_code_validation_rejects_wrong_behavior(tmp_path: Path) -> None:
    state = expected_states(1, 12)[-1]
    (tmp_path / "receipt.json").write_text(
        json.dumps({"state": state, "source_marker": "marker"}),
        encoding="utf-8",
    )
    (tmp_path / "solution.py").write_text(
        "def quote(amount, retries=0):\n    return 0\n", encoding="utf-8"
    )
    result = await validate_code(tmp_path, state, "marker")
    assert not result["success"]
    assert not result["checks"]["normal"]
