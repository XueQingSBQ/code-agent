"""Honest completion diagnostics and per-member Worker result collection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from evals.performance.completion_study import ARMS, diagnose, run, summarize
from evals.performance.worker_comparison import collect_wave

from carbide.agents.domain import SubagentResult, SubagentStatus


def result(identifier: str, *, failed: bool = False) -> SubagentResult:
    return SubagentResult(
        agent_id=identifier,
        parent_session_id="parent",
        status=SubagentStatus.FAILED if failed else SubagentStatus.COMPLETED,
    )


async def test_wave_persists_completed_member_before_another_times_out(tmp_path: Path) -> None:
    records = []
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def fast() -> SubagentResult:
        await slow_started.wait()
        return result("fast")

    async def slow() -> SubagentResult:
        slow_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            slow_cancelled.set()
        raise AssertionError("unreachable")

    def persist(completed: SubagentResult) -> None:
        records.append(completed.payload())
        (tmp_path / "results.json").write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await collect_wave([fast(), slow()], persist)
    assert slow_cancelled.is_set()
    saved = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [record["agent_id"] for record in saved] == ["fast"]


async def test_wave_keeps_failed_members_in_results() -> None:
    async def finished(failed: bool) -> SubagentResult:
        return result(str(failed), failed=failed)

    collected: list[SubagentResult] = []
    await collect_wave([finished(False), finished(True)], collected.append)
    assert {item.status for item in collected} == {SubagentStatus.COMPLETED, SubagentStatus.FAILED}


def test_diagnostics_preserve_failure_and_report_observed_repetition(tmp_path: Path) -> None:
    call = {"id": "1", "name": "edit", "input": {"old_text": "same", "new_text": "same"}}
    events = [
        {"type": "tool_use", "data": call},
        {"type": "tool_complete", "data": {"tool_id": "1", "is_error": True}},
        {"type": "tool_use", "data": {**call, "id": "2"}},
        {"type": "budget_warning", "data": {}},
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    record = {
        "instance_id": "case",
        "variant": "production",
        "exit_reason": "max_turns",
        "tests_passed": True,
        "success": False,
        "artifact_directory": str(tmp_path),
    }
    diagnostic = diagnose(record)
    assert not diagnostic["success"]
    assert diagnostic["patch_passed_without_clean_completion"]
    assert diagnostic["no_op_edits"] == 2
    assert diagnostic["observed_tool_errors"] == 1
    assert diagnostic["identical_repeated_calls"][0]["count"] == 2
    assert diagnostic["calls_without_completion_event"][0]["id"] == "2"
    assert diagnostic["budget_notices"] == 1


def test_summary_does_not_promote_partial_patch_or_pool_larger_budget() -> None:
    rows: list[dict[str, Any]] = [
        {
            "study_arm": "control",
            "success": False,
            "tests_passed": True,
            "exit_reason": "max_turns",
            "usage": {"complete": True},
        },
        {
            "study_arm": "guided",
            "success": False,
            "tests_passed": False,
            "exit_reason": "completed",
            "usage": {"complete": True},
        },
        {
            "study_arm": "extended",
            "success": True,
            "tests_passed": True,
            "exit_reason": "completed",
            "usage": {"complete": True},
        },
    ]
    summary = summarize(rows)
    assert summary["control"]["strict_successes"] == 0
    assert summary["guided"]["strict_successes"] == 0
    assert summary["extended"]["strict_successes"] == 1
    assert summary["control"]["patches_passing_independent_tests"] == 1


def test_control_and_guided_have_identical_limits() -> None:
    assert ARMS["control"]["max_turns"] == ARMS["guided"]["max_turns"] == 40
    assert ARMS["control"]["timeout_seconds"] == ARMS["guided"]["timeout_seconds"] == 420
    assert ARMS["extended"]["max_turns"] == 80
    assert ARMS["extended"]["timeout_seconds"] == 840
    assert not ARMS["extended"]["enable_budget_notices"]


async def test_study_refuses_to_overwrite_previous_evidence(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        await run(tmp_path / "missing.json", tmp_path / "missing.json", tmp_path)
