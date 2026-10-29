"""The evidence harness must preserve failures instead of polishing success rates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from evals.performance.evidence_report import collect, nearest_rank
from evals.performance.reliability import (
    RuntimeProvider,
    process_crash,
    run_trial,
    summarize,
)
from evals.performance.worker_comparison import ConcurrentMeter, metered_totals

from carbide.providers.base import StreamEnd, Usage


async def test_evaluator_preserves_scenario_exceptions(tmp_path: Path) -> None:
    async def fails(root: Path) -> dict[str, Any]:
        raise RuntimeError("injected grading failure")

    result = await run_trial("provider_disconnect_resume", tmp_path / "trial", 1, fails)
    assert result["passed"] is False
    assert "injected grading failure" in result["error"]
    assert result["elapsed_ms"] > 0


async def test_evaluator_does_not_score_capability_gap_as_success(tmp_path: Path) -> None:
    async def gap(root: Path) -> dict[str, Any]:
        return {
            "capability_gap": True,
            "queued_survived_reopen": True,
            "unacked_redelivered": False,
        }

    result = await run_trial("mailbox_unacked_redelivery", tmp_path / "trial", 1, gap)
    assert result["passed"] is False
    assert result["details"]["queued_survived_reopen"] is True
    assert result["details"]["unacked_redelivered"] is False


async def test_evaluator_injects_real_non_graceful_exit(tmp_path: Path) -> None:
    result = await run_trial("abrupt_process_exit_resume", tmp_path / "trial", 1, process_crash)
    assert result["passed"] is True, result.get("error")
    assert result["details"]["crash_exit_code"] == 23
    assert result["details"]["resume_ms"] > 0


def test_summary_includes_failures_in_denominator() -> None:
    result = summarize(
        [
            {"scenario": "provider_disconnect_resume", "passed": True, "elapsed_ms": 2},
            {"scenario": "provider_disconnect_resume", "passed": False, "elapsed_ms": 10},
        ]
    )["provider_disconnect_resume"]
    assert result == {"trials": 2, "passed": 1, "failed": 1, "p50_ms": 2.0, "p95_ms": 10.0}


async def test_evaluation_refuses_to_overwrite_evidence(tmp_path: Path) -> None:
    from evals.performance.reliability import evaluate

    with pytest.raises(FileExistsError):
        await evaluate(tmp_path, 1, ["provider_disconnect_resume"])


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"input_tokens": 0, "output_tokens": 8},
        {"input_tokens": 5, "cache_read_tokens": -1, "output_tokens": 8},
    ],
)
def test_live_metrics_reject_missing_or_zero_input(usage: dict[str, int] | None) -> None:
    assert metered_totals([{"usage": usage}])["complete"] is False


async def test_live_meter_tracks_actual_request_overlap(tmp_path: Path) -> None:
    class SlowProvider(RuntimeProvider):
        async def stream(
            self,
            messages: list[dict[str, Any]],
            system: Any = None,
            tools: Any = None,
            max_tokens: int = 4096,
            temperature: float = 1.0,
            **kwargs: Any,
        ) -> AsyncIterator[Any]:
            await asyncio.sleep(0.02)
            yield StreamEnd(usage=Usage(input_tokens=5, cache_read_tokens=10, output_tokens=1))

    meter = ConcurrentMeter(SlowProvider(), tmp_path)

    async def call() -> None:
        async for _ in meter.stream([{"role": "user", "content": "hi"}]):
            pass

    await asyncio.gather(call(), call())
    assert meter.peak_active == 2
    assert {request["id"] for request in meter.requests} == {0, 1}
    assert metered_totals(meter.requests)["prompt_tokens"] == 30
    assert metered_totals(meter.requests)["complete"] is True


def test_percentile_has_explicit_empty_sample_behavior() -> None:
    assert nearest_rank([], 0.95) is None
    assert nearest_rank([1, 4, 2, 3], 0.95) == 4


def test_final_report_rejects_incomplete_offline_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "evals.performance.evidence_report.read",
        lambda _path: {
            "environment": {"repetitions": 20, "scenarios": ["example"]},
            "records": [],
        },
    )
    with pytest.raises(ValueError, match="Incomplete reliability"):
        collect(tmp_path)


def test_final_report_rejects_partial_live_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_read(path: Path) -> Any:
        if path.parent.name == "reliability-final":
            return {"environment": {"repetitions": 1, "scenarios": ["example"]}, "records": [{}]}
        if path.name == "report.json":
            return {"status": "incomplete"}
        return []

    monkeypatch.setattr("evals.performance.evidence_report.read", fake_read)
    with pytest.raises(ValueError, match="Live experiments have not finished"):
        collect(tmp_path)
