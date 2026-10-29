"""Guard against optimistic scoring, hidden-test edits and sample inflation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from evals.context_efficiency.layered_ab import Request
from evals.context_efficiency.repository_ab import (
    Config,
    aggregate,
    bootstrap_ci,
    failed_worker_record,
    freeze_experiment,
    preserve_interruption,
    repository_usage,
    service_unavailable,
    source_patch,
    task_prompt,
)
from evals.context_efficiency.repository_environment import (
    checked,
    expected_status,
    grade,
    load_task_manifest,
    parse_junit,
    production_path,
    save_json,
)
from evals.context_efficiency.repository_report import render
from evals.context_efficiency.repository_session import load_sequence_manifest
from evals.swebench.instance import SWEBenchInstance


def instance(**overrides: Any) -> SWEBenchInstance:
    data: dict[str, Any] = {
        "instance_id": "sympy__fixture-1",
        "repo": "sympy/sympy",
        "base_commit": "base",
        "problem_statement": "The function should return one.",
        "hints_text": "hidden hint",
        "test_patch": "hidden regression",
        "patch": "hidden reference solution",
        "version": "1.5",
        "environment_setup_commit": "base",
        "FAIL_TO_PASS": ["test_value"],
        "PASS_TO_PASS": ["test_existing"],
    }
    data.update(overrides)
    return SWEBenchInstance(**data)


def test_missing_and_skipped_tests_never_pass() -> None:
    statuses = {"sympy.tests.test_feature::test_value": "skipped"}
    assert expected_status("test_value", statuses) == "skipped"
    assert expected_status("test_missing", statuses) == "missing"


def test_all_parameter_cases_must_pass() -> None:
    statuses = {
        "tests.test_feature::test_value[0]": "passed",
        "tests.test_feature::test_value[1]": "failed",
    }
    assert expected_status("tests/test_feature.py::test_value", statuses) == "failed"
    assert expected_status("tests/test_feature.py::test_value[0]", statuses) == "passed"


def test_qualified_ids_do_not_match_other_modules() -> None:
    statuses = {"tests.test_other::test_value": "passed"}
    assert expected_status("tests/test_feature.py::test_value", statuses) == "missing"


def test_class_and_truncated_official_parameter_ids_are_resolved_conservatively() -> None:
    statuses = {
        "tests.test_cli.TestRoutes::test_host": "passed",
        "tests.test_cli::test_locate_app[factory-one-1]": "passed",
        "tests.test_cli::test_locate_app[factory-two-2]": "failed",
    }
    assert expected_status("tests/test_cli.py::TestRoutes::test_host", statuses) == "passed"
    assert expected_status("tests/test_cli.py::test_locate_app[factory-", statuses) == "failed"


def test_junit_keeps_failures_collection_errors_and_skips(tmp_path: Path) -> None:
    report = tmp_path / "tests.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="m" name="ok"/>'
        '<testcase classname="m" name="bad"><failure/></testcase>'
        '<testcase classname="m" name="collect"><error/></testcase>'
        '<testcase classname="m" name="skip"><skipped/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert parse_junit(report) == {
        "m::ok": "passed",
        "m::bad": "failed",
        "m::collect": "error",
        "m::skip": "skipped",
    }


def test_prompt_never_leaks_reference_patch_or_hidden_tests() -> None:
    prompt = task_prompt(instance(), Path("C:/python/python.exe"))
    assert "hidden regression" not in prompt
    assert "hidden reference solution" not in prompt
    assert "hidden hint" not in prompt
    assert "test_value" not in prompt


def test_submission_cannot_change_tests_or_grader_configuration() -> None:
    task = instance()
    assert production_path(task, "sympy/core/function.py")
    assert not production_path(task, "sympy/core/tests/test_function.py")
    assert not production_path(task, "sympy/conftest.py")
    assert not production_path(task, "pytest.ini")
    assert not production_path(task, "../sympy/core/function.py")


def record(
    task: str, variant: str, repeat: int, tokens: int, success: bool, *, complete: bool = True
) -> dict[str, Any]:
    return {
        "instance_id": task,
        "variant": variant,
        "repeat": repeat,
        "success": success,
        "tests_passed": success,
        "usage": {
            "prompt_tokens": tokens,
            "input_tokens": tokens,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 5,
            "requests": 1,
            "complete": complete,
        },
        "requests": [{"purpose": "task"}],
        "agent_seconds": 1.0,
        "compressions": [],
        "layers": [],
        "pairing_violations": 0,
    }


def test_aggregate_keeps_failures_and_separates_pressure_profile() -> None:
    summary = aggregate(
        [
            record("a", "off", 1, 100, True),
            record("b", "off", 1, 100, True),
            record("a", "production", 1, 80, True),
            record("b", "production", 1, 10, False),
            record("a", "pressure", 1, 60, True),
        ]
    )
    assert summary["production"]["resolve_rate"] == 0.5
    assert summary["production"]["input_tokens"] == 90
    assert summary["production"]["both_successful_input_reduction"] == pytest.approx(0.2)
    assert summary["pressure"]["runs"] == 1
    assert summary["pressure"]["weighted_input_reduction"] == pytest.approx(0.4)


def test_incomplete_usage_is_not_treated_as_token_saving() -> None:
    summary = aggregate(
        [record("a", "off", 1, 100, True), record("a", "production", 1, 0, False, complete=False)]
    )
    assert summary["production"]["complete_pairs"] == 0
    assert summary["production"]["weighted_input_reduction"] is None


def test_bootstrap_unit_is_task_not_request_or_repeat() -> None:
    runs = []
    for repeat in range(1, 4):
        runs.extend(
            [record("a", "off", repeat, 100, True), record("a", "production", repeat, 50, True)]
        )
    assert aggregate(runs)["production"]["task_bootstrap_95_ci"] is None
    assert bootstrap_ci([0.1, 0.9]) == bootstrap_ci([0.1, 0.9])


def test_invalid_or_unpaired_experiment_is_rejected() -> None:
    with pytest.raises(ValueError):
        Config(variants=("production",))
    with pytest.raises(ValueError):
        Config(variants=("off", "production", "production"))
    with pytest.raises(ValueError):
        Config(repetitions=0)


def test_worker_crash_keeps_failure_without_inventing_measured_usage(tmp_path: Path) -> None:
    spec = {
        "instance": {"instance_id": "a", "repo": "sympy/sympy", "base_commit": "base"},
        "variant": "production",
        "repeat": 1,
        "timeout_seconds": 600,
    }
    failure = failed_worker_record(spec, tmp_path, "worker crashed")
    summary = aggregate([record("a", "off", 1, 100, True), failure])["production"]
    assert summary["runs"] == 1
    assert summary["successful"] == 0
    assert summary["usage_complete_runs"] == 0
    assert summary["weighted_input_reduction"] is None


def test_zero_input_stream_is_invalid_but_fully_cached_input_is_valid() -> None:
    def request(usage: dict[str, int] | None) -> Request:
        return Request("task", 1, "hash", 100, [], usage=usage)

    invalid = repository_usage([request({"input_tokens": 0, "output_tokens": 0})])
    assert not invalid["complete"]
    assert invalid["invalid_usage_requests"] == 1
    assert not repository_usage([])["complete"]
    valid = repository_usage([request({"input_tokens": 0, "cache_read_tokens": 100})])
    assert valid["complete"]
    assert valid["prompt_tokens"] == 100
    assert not repository_usage([request(None)])["complete"]


def test_parallelism_can_change_but_frozen_semantics_cannot(tmp_path: Path) -> None:
    frozen = {"config": {"parallel": 6, "max_turns": 60}, "runtime_sha256": "original"}
    freeze_experiment(tmp_path, frozen)
    freeze_experiment(tmp_path, {**frozen, "config": {"parallel": 1, "max_turns": 60}})
    with pytest.raises(ValueError):
        freeze_experiment(tmp_path, {**frozen, "runtime_sha256": "changed"})
    with pytest.raises(ValueError):
        freeze_experiment(tmp_path, {**frozen, "config": {"parallel": 1, "max_turns": 100}})


def test_interrupted_attempt_is_preserved_with_missing_request_accounted(tmp_path: Path) -> None:
    directory = tmp_path / "runs" / "issue" / "1" / "pressure"
    (directory / "repo").mkdir(parents=True)
    (directory / "repo" / "partial.txt").write_text("partial work", encoding="utf-8")
    save_json(directory / "requests" / "000.json", {"messages": []})
    preserve_interruption(tmp_path, directory)
    archived = list((tmp_path / "interrupted").glob("*/*/*/*"))
    assert len(archived) == 1
    assert not directory.exists()
    assert (archived[0] / "repo" / "partial.txt").read_text(encoding="utf-8") == "partial work"
    import json

    interruption = json.loads((archived[0] / "interruption.json").read_text(encoding="utf-8"))
    assert interruption["usage"]["requests"] == 1
    assert not interruption["usage"]["complete"]


def test_account_exhaustion_or_zero_usage_stops_dispatch_but_code_failure_does_not() -> None:
    assert service_unavailable({"errors": ["No available accounts"], "usage": {}})
    assert service_unavailable({"errors": [], "usage": {"invalid_usage_requests": 1}})
    assert not service_unavailable({"errors": [], "usage": {}, "success": False})


async def test_fresh_checkout_grading_baseline_fails_and_reference_passes(tmp_path: Path) -> None:
    repo = tmp_path / "original"
    module = repo / "sympy"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("def value():\n    return 0\n\n\n", encoding="utf-8")
    (module / "tests").mkdir()
    test = module / "tests" / "test_feature.py"
    test.write_text(
        "from sympy import value\n\ndef test_existing():\n    assert callable(value)\n",
        encoding="utf-8",
    )
    await checked(["git", "init"], cwd=repo)
    await checked(["git", "add", "."], cwd=repo)
    await checked(
        [
            "git",
            "-c",
            "user.name=Evaluation",
            "-c",
            "user.email=eval@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repo,
    )
    revision = await checked(["git", "rev-parse", "HEAD"], cwd=repo)
    test_patch = (
        "diff --git a/sympy/tests/test_feature.py b/sympy/tests/test_feature.py\n"
        "--- a/sympy/tests/test_feature.py\n+++ b/sympy/tests/test_feature.py\n"
        "@@ -2,3 +2,6 @@\n \n def test_existing():\n     assert callable(value)\n"
        "+\n+def test_value():\n+    assert value() == 1\n"
    )
    gold_patch = (
        "diff --git a/sympy/__init__.py b/sympy/__init__.py\n"
        "--- a/sympy/__init__.py\n+++ b/sympy/__init__.py\n"
        "@@ -1,4 +1,4 @@\n def value():\n-    return 0\n+    return 1\n \n \n"
    )
    task = instance(base_commit=revision, test_patch=test_patch, patch=gold_patch)
    baseline = await grade(task, repo, Path(sys.executable), tmp_path / "baseline")
    fixed = await grade(task, repo, Path(sys.executable), tmp_path / "fixed", gold=True)
    assert baseline["fail_to_pass"] == {"test_value": "failed"}
    assert baseline["pass_to_pass"] == {"test_existing": "passed"}
    assert not baseline["success"]
    assert fixed["success"]
    # Grading leaves the original checkout untouched.
    assert "return 0" in (module / "__init__.py").read_text(encoding="utf-8")
    (module / "__init__.py").write_text("def value():\n    return 1\n\n\n", encoding="utf-8")
    patch, files = await source_patch(task, repo)
    assert files == ["sympy/__init__.py"]
    assert patch.endswith(" \n")  # A valid empty context line must survive capture.
    captured = await grade(task, repo, Path(sys.executable), tmp_path / "captured", patch)
    assert captured["success"]


def test_fixed_manifest_is_ordered_and_rejects_unknown_or_duplicate_tasks(tmp_path: Path) -> None:
    tasks = [instance(instance_id="a", repo="sympy/sympy"), instance(instance_id="b")]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"schema_version": 1, "name": "fixture", "selection_policy": "fixed", '
        '"tasks": [{"instance_id": "b"}, {"instance_id": "a"}]}',
        encoding="utf-8",
    )
    metadata, resolved = load_task_manifest(manifest, tasks)
    assert metadata["name"] == "fixture"
    assert [item.instance_id for item, _ in resolved] == ["b", "a"]

    manifest.write_text(
        '{"schema_version": 1, "selection_policy": "fixed", '
        '"tasks": [{"instance_id": "a"}, {"instance_id": "a"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate"):
        load_task_manifest(manifest, tasks)

    manifest.write_text(
        '{"schema_version": 1, "selection_policy": "fixed", '
        '"tasks": [{"instance_id": "missing"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="absent"):
        load_task_manifest(manifest, tasks)


def test_report_labels_unrun_variants_explicitly(tmp_path: Path) -> None:
    selection = {
        "selected": [{"instance_id": "sympy__fixture-1", "repo": "sympy/sympy"}],
        "manifest": {"name": "fixture"},
        "selection_policy": "fixed",
        "original_issue_ids": [],
    }
    report = {
        "generated_at": "now",
        "config": {
            "repetitions": 1,
            "parallel": 1,
            "cases": 1,
            "variants": ["off", "pressure"],
            "max_turns": 1,
            "timeout_seconds": 1,
        },
        "task_ids": ["sympy__fixture-1"],
        "planned_runs": 2,
        "completed_runs": 1,
        "records": [
            {
                **record("sympy__fixture-1", "off", 1, 100, False),
                "model": "fixture",
                "validation": {"fail_to_pass": {}, "pass_to_pass": {}},
                "exit_reason": "completed",
                "errors": [],
            },
        ],
    }
    text = render(report, selection)
    assert "| 0/1 | not_run | not_run |" in text
    assert "按数据集顺序筛选" not in text


def test_sequence_manifest_preserves_stage_order_and_rejects_gaps(tmp_path: Path) -> None:
    manifest = tmp_path / "sequence.json"
    manifest.write_text(
        '{"schema_version": 1, "selection_policy": "fixed", "tasks": ['
        '{"stage": 1, "instance_id": "a"}, '
        '{"stage": 2, "instance_id": "b"}]}',
        encoding="utf-8",
    )
    loaded = load_sequence_manifest(manifest, {"a", "b"})
    assert [item["instance_id"] for item in loaded["tasks"]] == ["a", "b"]

    manifest.write_text(
        '{"schema_version": 1, "selection_policy": "fixed", "tasks": ['
        '{"stage": 2, "instance_id": "a"}, '
        '{"stage": 1, "instance_id": "b"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        load_sequence_manifest(manifest, {"a", "b"})
