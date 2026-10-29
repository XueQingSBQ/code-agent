"""Offline tests for the SWE-bench data and Docker harness boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from evals.swebench import runner
from evals.swebench.docker import DockerBashTool, DockerEnvironment, DockerResourceLimits
from evals.swebench.instance import SWEBenchInstance, load_instances
from evals.swebench.metrics import InstanceMetrics

from carbide.engine.loop import QueryEvent
from carbide.tools.base import ToolContext
from carbide.tools.shell import BashParams


def _dataset_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "base_commit": "a" * 40,
        "problem_statement": "fix it",
        "version": "3.12",
        "FAIL_TO_PASS": '["tests/test_demo.py::test_fix"]',
        "PASS_TO_PASS": ["tests/test_demo.py::test_existing"],
    }
    item.update(overrides)
    return item


def test_load_instances_normalizes_encoded_test_lists(tmp_path: Path) -> None:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps([_dataset_item()]), encoding="utf-8")

    instances = load_instances(path)

    assert instances[0].FAIL_TO_PASS == ["tests/test_demo.py::test_fix"]
    assert instances[0].PASS_TO_PASS == ["tests/test_demo.py::test_existing"]


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"not": "a list"}, "must contain a JSON list"),
        ([_dataset_item(FAIL_TO_PASS="not-json")], "FAIL_TO_PASS is not valid JSON"),
        ([_dataset_item(repo="")], "missing required fields"),
    ],
)
def test_load_instances_rejects_malformed_data(
    tmp_path: Path, payload: object, message: str
) -> None:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_instances(path)


@pytest.mark.asyncio
async def test_docker_run_tests_checks_each_test_exit_code(tmp_path: Path) -> None:
    instance = SWEBenchInstance(
        instance_id="demo__repo-1",
        repo="demo/repo",
        base_commit="a" * 40,
        problem_statement="fix it",
        hints_text="",
        test_patch="",
        patch="",
        version="3.12",
        environment_setup_commit="a" * 40,
        FAIL_TO_PASS=["test_pass"],
        PASS_TO_PASS=["test_fail"],
    )
    environment = DockerEnvironment(instance, tmp_path)
    environment.container_id = "container"

    async def fake_exec_status(command: list[str], cwd: str | None = None) -> tuple[str, str, int]:
        del cwd
        test_name = command[-1]
        return f"{test_name}\n", "", 0 if test_name == "test_pass" else 1

    environment._exec_status = fake_exec_status  # type: ignore[assignment]
    results = await environment.run_tests()

    assert results["passed_tests"] == ["test_pass"]
    assert results["failed_tests"] == ["test_fail"]
    assert results["success"] is False


def test_docker_shell_session_uses_container_transport(tmp_path: Path) -> None:
    environment = DockerEnvironment(_evaluation_instance(), tmp_path)
    environment.container_id = "container-id"

    session = environment.create_shell_session()

    assert session._command == [
        "docker",
        "exec",
        "-i",
        "-w",
        "/workspace/repo",
        "container-id",
        "bash",
        "-l",
    ]
    assert session._process_cwd is None


def test_docker_run_command_contains_resource_limits(tmp_path: Path) -> None:
    limits = DockerResourceLimits(cpus=0.5, memory="768m", pids_limit=64)
    environment = DockerEnvironment(_evaluation_instance(), tmp_path, limits)

    command = environment._build_run_command()

    assert command[command.index("--cpus") + 1] == "0.5"
    assert command[command.index("--memory") + 1] == "768m"
    assert command[command.index("--pids-limit") + 1] == "64"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"cpus": 0}, "CPU"),
        ({"cpus": float("inf")}, "CPU"),
        ({"memory": "4g;rm"}, "memory"),
        ({"pids_limit": 0}, "PID"),
    ],
)
def test_docker_resource_limits_reject_invalid_values(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DockerResourceLimits(**kwargs)


class _FakeDockerEnvironment:
    """Offline Docker double that records the lifecycle used by the runner."""

    instances: ClassVar[list[_FakeDockerEnvironment]] = []
    test_results: ClassVar[dict[str, Any]] = {
        "success": True,
        "passed_tests": ["test_pass", "test_existing"],
        "failed_tests": [],
    }

    def __init__(self, instance: SWEBenchInstance, work_dir: Path) -> None:
        self.instance = instance
        self.work_dir = work_dir
        self.setup_called = False
        self.patch_called = False
        self.tests_called = False
        self.cleanup_called = False
        self.__class__.instances.append(self)

    async def setup(self) -> None:
        self.setup_called = True

    async def apply_test_patch(self) -> None:
        self.patch_called = True

    async def run_tests(self) -> dict[str, Any]:
        self.tests_called = True
        return self.test_results

    async def cleanup(self) -> None:
        self.cleanup_called = True

    def create_shell_session(self) -> None:
        return None


class _FakeShellSession:
    def __init__(self, output: str = "", exit_code: int = 0) -> None:
        self.output = output
        self.exit_code = exit_code
        self.commands: list[tuple[str, float]] = []

    async def execute(self, command: str, **kwargs: float) -> tuple[str, int]:
        timeout = kwargs.get("timeout", 120.0)
        self.commands.append((command, timeout))
        return self.output, self.exit_code


class _FakeShellEnvironment:
    def __init__(self, session: _FakeShellSession) -> None:
        self.session = session

    def create_shell_session(self) -> _FakeShellSession:
        return self.session


class _FakeAgent:
    """Offline Agent double emitting the events consumed by the evaluator."""

    instances: ClassVar[list[_FakeAgent]] = []
    reason: ClassVar[str] = "completed"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    async def __aenter__(self) -> _FakeAgent:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def query(self, message: str, tools: list[dict[str, Any]] | None = None) -> Any:
        del message, tools

        async def events() -> Any:
            yield QueryEvent(type="tool_complete", data={"name": "edit"})
            yield QueryEvent(
                type="turn_complete",
                data={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 5,
                    "reason": self.reason,
                },
            )

        return events()


def _evaluation_instance() -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id="demo__repo-1",
        repo="demo/repo",
        base_commit="a" * 40,
        problem_statement="fix it",
        hints_text="",
        test_patch="test patch",
        patch="",
        version="3.12",
        environment_setup_commit="a" * 40,
        FAIL_TO_PASS=["test_pass"],
        PASS_TO_PASS=["test_existing"],
    )


@pytest.mark.asyncio
async def test_docker_bash_tool_maps_output_and_exit_status(tmp_path: Path) -> None:
    session = _FakeShellSession(output="inside container", exit_code=0)
    tool = DockerBashTool(_FakeShellEnvironment(session))  # type: ignore[arg-type]

    result = await tool.run(
        BashParams(command="printf inside container", timeout=3),
        ToolContext(workspace=tmp_path),
    )

    assert result.content == "inside container"
    assert result.is_error is False
    assert session.commands == [("printf inside container", 3.0)]

    failing_session = _FakeShellSession(output="failed", exit_code=7)
    failing_tool = DockerBashTool(_FakeShellEnvironment(failing_session))  # type: ignore[arg-type]
    failed = await failing_tool.run(
        BashParams(command="false"),
        ToolContext(workspace=tmp_path),
    )
    assert failed.content == "exit 7\nfailed"
    assert failed.is_error is True


@pytest.mark.asyncio
async def test_evaluate_instance_collects_events_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeDockerEnvironment.instances.clear()
    _FakeAgent.instances.clear()
    _FakeAgent.reason = "completed"
    monkeypatch.setattr(runner, "DockerEnvironment", _FakeDockerEnvironment)
    monkeypatch.setattr(runner, "Agent", _FakeAgent)

    metrics = await runner.evaluate_instance(
        _evaluation_instance(), tmp_path / "work", "test-key", "test-model"
    )

    assert metrics.resolved is True
    assert metrics.error is None
    assert metrics.num_turns == 1
    assert metrics.num_tool_calls == 1
    assert metrics.total_tokens == 120
    assert metrics.cached_tokens == 5
    environment = _FakeDockerEnvironment.instances[0]
    assert environment.setup_called is True
    assert environment.patch_called is True
    assert environment.tests_called is True
    assert environment.cleanup_called is True
    assert _FakeAgent.instances[0].kwargs["enable_memory"] is False
    assert _FakeAgent.instances[0].kwargs["enable_skills"] is False
    assert _FakeAgent.instances[0].kwargs["enable_rules"] is False


@pytest.mark.asyncio
async def test_evaluate_instance_preserves_agent_termination_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeDockerEnvironment.instances.clear()
    _FakeAgent.instances.clear()
    _FakeAgent.reason = "max_turns"
    monkeypatch.setattr(runner, "DockerEnvironment", _FakeDockerEnvironment)
    monkeypatch.setattr(runner, "Agent", _FakeAgent)

    metrics = await runner.evaluate_instance(
        _evaluation_instance(), tmp_path / "work", "test-key", "test-model"
    )

    assert metrics.resolved is False
    assert metrics.error == "Agent terminated with reason: max_turns"
    environment = _FakeDockerEnvironment.instances[0]
    assert environment.patch_called is False
    assert environment.tests_called is False
    assert environment.cleanup_called is True


@pytest.mark.asyncio
async def test_run_evaluation_passes_timeout_and_writes_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps([_dataset_item()]), encoding="utf-8")
    observed: dict[str, int] = {}

    async def fake_evaluate_instance(
        instance: SWEBenchInstance,
        work_dir: Path,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: int = 1800,
    ) -> InstanceMetrics:
        del work_dir, api_key, model, base_url
        observed[instance.instance_id] = timeout_seconds
        now = datetime.now(UTC)
        return InstanceMetrics(
            instance_id=instance.instance_id,
            start_time=now,
            end_time=now,
            resolved=True,
        )

    monkeypatch.setattr(runner, "evaluate_instance", fake_evaluate_instance)

    aggregate = await runner.run_evaluation(
        dataset,
        tmp_path / "results",
        "test-key",
        "test-model",
        max_instances=1,
        parallel=1,
        timeout_seconds=7,
    )

    assert aggregate.total_instances == 1
    assert aggregate.resolved_instances == 1
    assert observed == {"demo__repo-1": 7}
    assert (tmp_path / "results" / "demo__repo-1.json").exists()
    report = json.loads((tmp_path / "results" / "aggregate_report.json").read_text())
    assert report["aggregate"]["resolved_instances"] == 1


@pytest.mark.asyncio
async def test_run_evaluation_rejects_invalid_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parallel"):
        await runner.run_evaluation(
            tmp_path / "missing.json",
            tmp_path / "results",
            "test-key",
            "test-model",
            parallel=0,
        )

    with pytest.raises(ValueError, match="max_instances"):
        await runner.run_evaluation(
            tmp_path / "missing.json",
            tmp_path / "results",
            "test-key",
            "test-model",
            max_instances=-1,
        )

    with pytest.raises(ValueError, match="timeout_seconds"):
        await runner.run_evaluation(
            tmp_path / "missing.json",
            tmp_path / "results",
            "test-key",
            "test-model",
            timeout_seconds=0,
        )
