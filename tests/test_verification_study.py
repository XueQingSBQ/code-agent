"""Keep task inputs, execution completion, and independent correctness separate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.context_efficiency.repository_ab import (
    service_unavailable,
    task_prompt,
    verify_frozen_inputs,
)
from evals.context_efficiency.repository_environment import save_json
from evals.performance import verification_study as study

from carbide.providers.base import StreamEnd, StreamTextDelta
from carbide.sdk import Agent
from tests.conftest import ScriptedProvider
from tests.test_repository_evaluation import instance


def selection(tmp_path: Path) -> dict[str, Any]:
    return {
        "selected": [
            {
                **vars(instance(instance_id=identifier, hints_text="Public discussion.")),
                "cache": str(tmp_path),
                "python": str(tmp_path),
            }
            for identifier in study.CASES
        ]
    }


def test_matrix_separates_input_and_workflow_with_identical_budgets(tmp_path: Path) -> None:
    specs = study.build_matrix(selection(tmp_path), 2)
    assert len(specs) == 16
    assert specs == study.build_matrix(selection(tmp_path), 2)
    for identifier in study.CASES:
        for repeat in (1, 2):
            cells = [
                s
                for s in specs
                if s["instance"]["instance_id"] == identifier and s["repeat"] == repeat
            ]
            assert {s["study_arm"] for s in cells} == set(study.ARMS)
            assert {
                (
                    s["max_turns"],
                    s["timeout_seconds"],
                    s["variant"],
                    s["enable_budget_notices"],
                    s["deadline_hints"],
                )
                for s in cells
            } == {(40, 420, "production", True, True)}
            for hints in (False, True):
                pair = [s for s in cells if s["include_public_hints"] == hints]
                assert len(pair) == 2
                inputs = [
                    task_prompt(
                        instance(**{key: s["instance"][key] for key in vars(instance())}),
                        Path("python"),
                        include_public_hints=hints,
                    )
                    for s in pair
                ]
                assert inputs[0] == inputs[1]
                assert pair[0]["system_prompt"] != pair[1]["system_prompt"]


@pytest.mark.parametrize("hints", [False, True])
def test_assisted_track_exposes_only_opted_in_discussion(hints: bool) -> None:
    task = instance(hints_text="Public root-cause discussion.")
    prompt = task_prompt(task, Path("python"), include_public_hints=hints)
    assert (task.hints_text in prompt) == hints
    assert task.patch not in prompt
    assert task.test_patch not in prompt
    assert all(name not in prompt for name in task.FAIL_TO_PASS + task.PASS_TO_PASS)


@pytest.mark.parametrize("field", ["runtime", "harness", "prompt", "system_prompt"])
def test_worker_rejects_frozen_protocol_drift(tmp_path: Path, field: str) -> None:
    spec = study.build_matrix(selection(tmp_path), 1)[0]
    spec[f"expected_{field}_sha256"] = "altered"
    with pytest.raises(ValueError, match=f"Frozen {field} changed"):
        verify_frozen_inputs(spec)


def test_unattempted_cells_have_no_success_rate_and_partial_patch_is_not_success() -> None:
    rows = [
        {
            "study_arm": "problem_only_before",
            "success": False,
            "tests_passed": True,
            "exit_reason": "max_turns",
        },
        {
            "study_arm": "discussion_acceptance",
            "success": False,
            "tests_passed": False,
            "exit_reason": "completed",
        },
    ]
    summary = study.summarize(rows)
    assert summary["problem_only_before"]["strict_successes"] == 0
    assert summary["problem_only_before"]["patches_passing_independent_tests"] == 1
    assert summary["discussion_acceptance"]["strict_success_rate"] == 0
    assert summary["discussion_before"]["strict_success_rate"] is None
    assert summary["discussion_before"]["attempts"] == 0


async def test_endpoint_failure_does_not_launch_or_score_model_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selection.json"
    save_json(path, selection(tmp_path))

    async def unavailable() -> dict[str, Any]:
        return {"available": False, "error": "No available channel"}

    async def no_worker(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No model worker may start after failed preflight")

    monkeypatch.setattr(study, "probe", unavailable)
    monkeypatch.setattr(study, "command", no_worker)
    report = await study.run(path, tmp_path / "out", live=True)
    assert report["status"] == "endpoint_unavailable"
    assert report["planned"] == 16 and report["attempted"] == 0
    assert report["records"] == []
    assert all(row["strict_success_rate"] is None for row in report["summary"].values())
    with pytest.raises(FileExistsError):
        await study.run(path, tmp_path / "out", live=True)


async def test_offline_mode_never_calls_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "selection.json"
    save_json(path, selection(tmp_path))

    async def no_probe() -> dict[str, Any]:
        raise AssertionError("Offline audits must not contact the provider")

    monkeypatch.setattr(study, "probe", no_probe)
    report = await study.run(path, tmp_path / "out")
    assert report["status"] == "offline_complete_live_not_run"
    assert report["attempted"] == 0


async def test_audit_retains_original_failure_and_uses_frozen_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "history"
    spec = study.build_matrix(selection(tmp_path), 1)[0]
    save_json(source / "spec.json", spec)
    (source / "submission.patch").write_text("candidate production patch", encoding="utf-8")
    (source / "prompt.txt").write_text("problem only", encoding="utf-8")
    original = {"success": False, "exit_reason": "completed", "tests_passed": False}
    monkeypatch.setattr(study, "assessed_record", lambda _: original)

    async def regrade(*args: Any) -> dict[str, Any]:
        assert args[-1] == "candidate production patch"
        assert args[-2] == tmp_path / "audit" / "grading"
        return {"success": False, "fail_to_pass": {"target": "failed"}}

    monkeypatch.setattr(study, "grade", regrade)
    result = await study.audit_run(source, tmp_path / "audit")
    assert not result["original_success"]
    assert result["original_exit_reason"] == "completed"
    assert not result["regraded_validation"]["success"]
    assert not result["public_discussion_in_original_prompt"]
    assert json.loads((tmp_path / "audit" / "audit.json").read_text())[
        "patch_sha256"
    ] == study.digest("candidate production patch")
    assert original == {"success": False, "exit_reason": "completed", "tests_passed": False}
    with pytest.raises(FileExistsError):
        await study.audit_run(source, tmp_path / "audit")


def test_no_available_model_channel_stops_further_attempts() -> None:
    assert service_unavailable({"errors": ["503 model_not_found: No available channel for model"]})
    assert not service_unavailable({"errors": ["Tool command returned nonzero"]})


async def test_normal_conversation_and_custom_prompt_keep_natural_completion(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([[StreamTextDelta("hello"), StreamEnd()]])
    async with Agent(
        provider=provider,
        project_dir=tmp_path,
        system_prompt="custom instructions",
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
        enable_subagents=False,
    ) as agent:
        events = [event async for event in agent.query("hello")]
        assert agent.system_prompt == "custom instructions"
        assert events[-1].type == "turn_complete"
        assert events[-1].data["reason"] == "completed"
        assert provider.call_count == 1
