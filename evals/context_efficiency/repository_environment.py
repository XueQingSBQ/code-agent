"""Reproducible native environments and independent SWE-bench test grading.

This is a local subset evaluation, not the official containerized leaderboard
harness. Candidate patches are graded in fresh checkouts; model changes to tests
and test configuration never enter the grading checkout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evals.swebench.instance import SWEBenchInstance, load_instances

DATASET_URL = (
    "https://datasets-server.huggingface.co/rows?dataset=princeton-nlp%2FSWE-bench_Lite"
    "&config=default&split=test&length=100&offset="
)
PROFILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "sympy": (
        "3.9",
        ("pytest==6.2.5", "py==1.8.2", "mpmath==1.2.1", "numpy==1.26.4", "certifi==2026.7.22"),
    ),
    "flask-2.0-py39": (
        "3.9",
        (
            "pytest==7.4.4",
            "Werkzeug==2.0.3",
            "Jinja2==3.0.3",
            "itsdangerous==2.0.1",
            "click==8.0.4",
        ),
    ),
    "flask-2.3-py311": (
        "3.11",
        (
            "pytest==7.4.4",
            "Werkzeug==2.3.8",
            "Jinja2==3.1.6",
            "itsdangerous==2.1.2",
            "click==8.1.7",
            "blinker==1.6.3",
            "asgiref==3.8.1",
            "python-dotenv==1.0.1",
            "Flask==2.3.2",
        ),
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def command(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 180,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run bounded argv commands and reap children on cancellation/timeout."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        elif process.returncode is None:
            process.kill()
        stdout, stderr = await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return CommandResult(
            -1, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"), True
        )
    return CommandResult(
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def checked(argv: list[str], *, cwd: Path | None = None, timeout_seconds: float = 180) -> str:
    result = await command(argv, cwd=cwd, timeout_seconds=timeout_seconds)
    if result.returncode:
        raise RuntimeError(f"{argv[:3]}: {result.stderr[-1600:]} {result.stdout[-1600:]}")
    return result.stdout.strip()


def save_json(path: Path, value: Any) -> None:
    """Atomically persist generated evaluation artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def download_dataset(destination: Path) -> None:
    rows: list[dict[str, Any]] = []
    for offset in (0, 100, 200):
        with urllib.request.urlopen(DATASET_URL + str(offset), timeout=60) as response:
            page = json.load(response)
        rows.extend(item["row"] for item in page["rows"])
    if len(rows) != 300 or len({row["instance_id"] for row in rows}) != 300:
        raise ValueError("Incomplete SWE-bench Lite dataset")
    save_json(destination, rows)


def profile(instance: SWEBenchInstance) -> str:
    if instance.repo == "sympy/sympy":
        return "sympy"
    return f"flask-{instance.version}-{'py39' if instance.version == '2.0' else 'py311'}"


def source_root(instance: SWEBenchInstance) -> str:
    return "sympy" if instance.repo == "sympy/sympy" else "src/flask"


def production_path(instance: SWEBenchInstance, name: str) -> bool:
    path = Path(name)
    root = source_root(instance)
    return (
        name.startswith(root + "/")
        and path.suffix == ".py"
        and "tests" not in path.parts
        and ".." not in path.parts
        and path.name != "conftest.py"
    )


async def make_environment(root: Path, name: str) -> Path:
    directory = root / "environments" / name
    python = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    version, packages = PROFILES[name]
    if python.exists() and (directory / "ready.json").exists():
        ready = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
        if ready["packages"] == list(packages):
            return python
    if not python.exists():
        await checked(["uv", "venv", "--python", version, str(directory)], timeout_seconds=180)
    await checked(["uv", "pip", "install", "--python", str(python), *packages], timeout_seconds=240)
    freeze = await checked(["uv", "pip", "freeze", "--python", str(python)])
    save_json(directory / "ready.json", {"python": version, "packages": packages, "freeze": freeze})
    return python


async def checkout(cache: Path, instance: SWEBenchInstance, destination: Path) -> None:
    if await asyncio.to_thread(destination.exists):
        raise FileExistsError(f"Refusing to overwrite evaluation checkout: {destination}")
    await checked(["git", "clone", "--shared", "--no-checkout", str(cache), str(destination)])
    # A shared clone of a partial bare clone loses its promisor configuration.
    # Only snapshot hydration needs remote fetching; later attempts share the
    # already-hydrated snapshot and never fetch historical blobs again.
    partial = await command(["git", "config", "--get", "remote.origin.promisor"], cwd=cache)
    if partial.stdout.strip() == "true":
        await checked(
            ["git", "config", "remote.origin.url", f"https://github.com/{instance.repo}.git"],
            cwd=destination,
        )
        await checked(["git", "config", "remote.origin.promisor", "true"], cwd=destination)
        await checked(
            ["git", "config", "remote.origin.partialclonefilter", "blob:none"], cwd=destination
        )
    # Force is confined to this newly-created checkout, never an existing tree.
    revision = await command(
        ["git", "rev-parse", "--verify", instance.base_commit + "^{commit}"], cwd=destination
    )
    ref = instance.base_commit if revision.returncode == 0 else "HEAD"
    await checked(
        ["git", "checkout", "--detach", "--force", ref],
        cwd=destination,
        timeout_seconds=300,
    )
    deleted = await checked(["git", "ls-files", "--deleted"], cwd=destination)
    if deleted:
        raise RuntimeError("Incomplete snapshot hydration")


def download_archive(instance: SWEBenchInstance, destination: Path, cafile: str) -> None:
    """Download pinned source without relying on partial-clone lazy fetching."""
    url = f"https://codeload.github.com/{instance.repo}/tar.gz/{instance.base_commit}"
    context = ssl.create_default_context(cafile=cafile)
    for retry in range(3):
        try:
            with (
                urllib.request.urlopen(url, timeout=90, context=context) as response,
                tempfile.TemporaryFile() as archive_file,
            ):
                while chunk := response.read(1024 * 1024):
                    archive_file.write(chunk)
                archive_file.seek(0)
                with tarfile.open(fileobj=archive_file, mode="r:gz") as archive:
                    for member in archive.getmembers():
                        parts = Path(member.name).parts[1:]
                        if not parts:
                            continue
                        member.name = str(Path(*parts))
                        archive.extract(member, destination, filter="data")
            return
        except OSError:
            if retry == 2:
                raise
            time.sleep(1)


async def archive_snapshot(
    root: Path, instance: SWEBenchInstance, upstream: Path, cafile: str
) -> Path:
    """Verify archived Python blobs against the original Git commit tree."""
    directory = root / "snapshots" / "archive" / instance.instance_id
    workspace = directory / "repo"
    ready = directory / "ready.json"
    if ready.exists():
        return workspace
    retries = root / "snapshots" / "archive-retry" / instance.instance_id
    for retry_ready in sorted(retries.glob("*/ready.json")):
        metadata = json.loads(retry_ready.read_text(encoding="utf-8"))
        if metadata["base_commit"] == instance.base_commit:
            return retry_ready.parent / "repo"
    if workspace.exists() and any(workspace.iterdir()):
        # Interrupted downloads are diagnostic artifacts, never overwritten.
        directory = (
            root / "snapshots" / "archive-retry" / instance.instance_id / str(time.time_ns())
        )
        workspace = directory / "repo"
        ready = directory / "ready.json"
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(download_archive, instance, workspace, cafile)
    await checked(["git", "init"], cwd=workspace)
    await checked(["git", "config", "core.autocrlf", "false"], cwd=workspace)
    await checked(["git", "add", "--force", "."], cwd=workspace)
    expected_tree = await checked(["git", "ls-tree", "-r", instance.base_commit], cwd=upstream)
    actual_tree = await checked(["git", "ls-files", "--stage"], cwd=workspace)
    expected = {line.split("\t", 1)[1]: line.split()[2] for line in expected_tree.splitlines()}
    actual = {line.split("\t", 1)[1]: line.split()[1] for line in actual_tree.splitlines()}
    critical = {
        name: sha
        for name, sha in expected.items()
        if name.endswith(".py")
        and (name.startswith(source_root(instance) + "/") or name.startswith("tests/"))
    }
    if not critical or any(actual.get(name) != sha for name, sha in critical.items()):
        raise ValueError("Archived source/test blobs do not match pinned upstream commit")
    await checked(
        [
            "git",
            "-c",
            "user.name=Evaluation",
            "-c",
            "user.email=eval@example.invalid",
            "commit",
            "-m",
            f"Pinned snapshot {instance.base_commit}",
        ],
        cwd=workspace,
    )
    snapshot_commit = await checked(["git", "rev-parse", "HEAD"], cwd=workspace)
    save_json(
        ready,
        {
            "base_commit": instance.base_commit,
            "snapshot_commit": snapshot_commit,
            "verified_python_blobs": len(critical),
        },
    )
    return workspace


async def apply_patch_file(workspace: Path, patch: str, destination: Path) -> None:
    if not patch.strip():
        return
    await asyncio.to_thread(destination.write_text, patch, encoding="utf-8", newline="\n")
    patch_path = await asyncio.to_thread(destination.resolve)
    await checked(["git", "apply", "--whitespace=nowarn", str(patch_path)], cwd=workspace)


def test_files(instance: SWEBenchInstance) -> list[str]:
    paths = re.findall(r"^\+\+\+ b/(.+)$", instance.test_patch, flags=re.MULTILINE)
    return sorted(
        {name for name in paths if Path(name).name.startswith("test") and name.endswith(".py")}
    )


def parse_junit(path: Path) -> dict[str, str]:
    """Keep every case status, including skips and collection errors."""
    if not path.is_file():
        return {}
    statuses: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        status = "passed"
        for tag, value in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            if case.find(tag) is not None:
                status = value
                break
        statuses[f"{classname}::{name}"] = status
    return statuses


def load_task_manifest(
    path: Path, instances: list[SWEBenchInstance]
) -> tuple[dict[str, Any], list[tuple[SWEBenchInstance, dict[str, Any]]]]:
    """Resolve a fixed, reviewable task manifest against the downloaded dataset."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    tasks = manifest.get("tasks")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("selection_policy"), str)
        or not isinstance(tasks, list)
        or not tasks
    ):
        raise ValueError(
            "Task manifest must use schema_version=1, define selection_policy and contain tasks"
        )

    by_id = {instance.instance_id: instance for instance in instances}
    resolved: list[tuple[SWEBenchInstance, dict[str, Any]]] = []
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("instance_id"), str):
            raise ValueError("Every manifest task must contain a string instance_id")
        instance_id = task["instance_id"]
        if instance_id in seen:
            raise ValueError(f"Duplicate task in manifest: {instance_id}")
        if instance_id not in by_id:
            raise ValueError(f"Manifest task is absent from dataset: {instance_id}")
        source_files = {
            match.group(1)
            for match in re.finditer(r"^diff --git a/(.*?) b/", by_id[instance_id].patch, re.MULTILINE)
        }
        expected_source_files = task.get("expected_source_files")
        if expected_source_files is not None and len(source_files) != expected_source_files:
            raise ValueError(
                f"Manifest source-file count drift for {instance_id}: "
                f"expected {expected_source_files}, got {len(source_files)}"
            )
        max_pass_to_pass = task.get("max_pass_to_pass")
        if max_pass_to_pass is not None and len(by_id[instance_id].PASS_TO_PASS) > max_pass_to_pass:
            raise ValueError(
                f"Manifest regression scope drift for {instance_id}: "
                f"expected at most {max_pass_to_pass}, got {len(by_id[instance_id].PASS_TO_PASS)}"
            )
        seen.add(instance_id)
        resolved.append((by_id[instance_id], task))
    return manifest, resolved


def expected_status(expected: str, statuses: dict[str, str]) -> str:
    """Resolve official IDs without treating a missing/skipped case as passing."""
    parts = expected.split("::")
    suffix = parts[-1]
    module = parts[0].removesuffix(".py").replace("/", ".") if len(parts) > 1 else None
    if module is not None and len(parts) > 2:
        module += "." + ".".join(parts[1:-1])

    def matches_name(name: str) -> bool:
        if name == suffix:
            return True
        if "[" not in suffix:
            return name.startswith(suffix + "[")
        # Some exported official IDs stop at a space in a parameter. Require
        # every case matching that exact prefix, never just one passing case.
        return not suffix.endswith("]") and name.startswith(suffix)

    matches = [
        value
        for key, value in statuses.items()
        if matches_name(key.split("::", 1)[-1])
        and (module is None or key.split("::", 1)[0] == module)
    ]
    if not matches:
        return "missing"
    return next((value for value in matches if value != "passed"), "passed")


async def grade(
    instance: SWEBenchInstance,
    cache: Path,
    python: Path,
    directory: Path,
    candidate_patch: str = "",
    *,
    gold: bool = False,
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    workspace = directory / "repo"
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    try:
        await checkout(cache, instance, workspace)
        await apply_patch_file(
            workspace, instance.patch if gold else candidate_patch, directory / "source.patch"
        )
        await apply_patch_file(workspace, instance.test_patch, directory / "test.patch")
        files = test_files(instance)
        if not files or not instance.FAIL_TO_PASS:
            raise ValueError("No independent regression tests")
        env = dict(os.environ)
        env["PYTHONPATH"] = (
            str(workspace / "src") if instance.repo == "pallets/flask" else str(workspace)
        )
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        junit = directory / "tests.xml"
        # SymPy's tests are ordinary pytest tests in the selected revisions.
        result = await command(
            [
                str(python),
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "-o",
                "addopts=",
                f"--junitxml={junit}",
                *files,
            ],
            cwd=workspace,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        (directory / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (directory / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        statuses = parse_junit(junit)
        targets = {name: expected_status(name, statuses) for name in instance.FAIL_TO_PASS}
        regressions = {name: expected_status(name, statuses) for name in instance.PASS_TO_PASS}
        expected = {**targets, **regressions}
        return {
            "success": bool(expected)
            and all(value == "passed" for value in expected.values())
            and not result.timed_out,
            "fail_to_pass": targets,
            "pass_to_pass": regressions,
            "cases": statuses,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "files": files,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fail_to_pass": {},
            "pass_to_pass": {},
        }


async def prepare(
    root: Path, count: int = 20, manifest_path: Path | None = None
) -> dict[str, Any]:
    """Freeze cases using environment checks only, before seeing model results."""
    dataset = root / "dataset.json"
    if not dataset.exists():
        await asyncio.to_thread(download_dataset, dataset)
    instances = load_instances(dataset)
    caches: dict[str, Path] = {}
    for repo in ("sympy/sympy", "pallets/flask"):
        cache = root / "upstream" / repo.replace("/", "--")
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            await checked(
                [
                    "git",
                    "clone",
                    "--bare",
                    "--filter=blob:none",
                    f"https://github.com/{repo}.git",
                    str(cache),
                ],
                timeout_seconds=300,
            )
        caches[repo] = cache
    environments = {name: await make_environment(root, name) for name in PROFILES}
    cafile = await checked(
        [str(environments["sympy"]), "-c", "import certifi; print(certifi.where())"]
    )
    selected: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    manifest: dict[str, Any] | None = None
    metadata_by_id: dict[str, dict[str, Any]] = {}
    manifest_location = str(manifest_path) if manifest_path is not None else None
    if manifest_path is not None:
        manifest, manifest_tasks = load_task_manifest(manifest_path, instances)
        candidates = [item for item, _metadata in manifest_tasks]
        metadata_by_id = {item.instance_id: metadata for item, metadata in manifest_tasks}
        target_count = len(candidates)
        preflight_name = f"manifest-v{manifest['schema_version']}"
    else:
        candidates = [item for item in instances if item.repo == "pallets/flask"]
        candidates += [
            item
            for item in instances
            if item.repo == "sympy/sympy" and tuple(map(int, item.version.split("."))) >= (1, 5)
        ]
        target_count = count
        preflight_name = "compatible-v3"

    async def check_case(instance: SWEBenchInstance) -> tuple[dict[str, Any], Path]:
        directory = root / "preflight" / preflight_name / instance.instance_id
        snapshot = await archive_snapshot(root, instance, caches[instance.repo], cafile)
        preflight_file = directory / "result.json"
        if preflight_file.exists():
            record = json.loads(preflight_file.read_text(encoding="utf-8"))
        else:
            python = environments[profile(instance)]
            attempt_directory = directory / f"attempt-{time.time_ns()}"
            baseline = await grade(instance, snapshot, python, attempt_directory / "base")
            reference = await grade(
                instance, snapshot, python, attempt_directory / "gold", gold=True
            )
            target_statuses = baseline.get("fail_to_pass", {}).values()
            eligible = (
                reference["success"]
                and any(status == "failed" for status in target_statuses)
                and all(value == "passed" for value in baseline.get("pass_to_pass", {}).values())
                and not baseline.get("timed_out")
            )
            record = {
                "instance_id": instance.instance_id,
                "eligible": eligible,
                "base": baseline,
                "gold": reference,
            }
            save_json(preflight_file, record)
        return record, snapshot

    for offset in range(0, len(candidates), 4):
        if len(selected) >= target_count:
            break
        batch = candidates[offset : offset + 4]
        results = await asyncio.gather(*(check_case(item) for item in batch))
        for instance, (record, snapshot) in zip(batch, results, strict=True):
            checks.append(record)
            if record["eligible"] and len(selected) < target_count:
                selected.append(
                    {
                        **asdict(instance),
                        "python": str(environments[profile(instance)]),
                        "cache": str(snapshot),
                        **(
                            {"selection_metadata": metadata_by_id[instance.instance_id]}
                            if instance.instance_id in metadata_by_id
                            else {}
                        ),
                    }
                )
            print(
                f"PREFLIGHT {instance.instance_id} eligible={record['eligible']} selected={len(selected)}/{target_count}",
                flush=True,
            )
        if manifest is not None:
            save_json(
                root / "selection.json",
                {
                    "requested": target_count,
                    "selected": selected,
                    "checks": checks,
                    "manifest": manifest,
                    "manifest_path": manifest_location,
                    "selection_policy": manifest["selection_policy"],
                },
            )
        else:
            save_json(root / "selection.json", {"requested": count, "selected": selected, "checks": checks})
    if len(selected) < target_count:
        raise RuntimeError(
            f"Only {len(selected)} environment-valid cases out of requested {target_count}; exclusions saved"
        )
    result: dict[str, Any] = {"requested": target_count, "selected": selected, "checks": checks}
    if manifest is not None:
        result.update(
            {
                "manifest": manifest,
                "manifest_path": manifest_location,
                "selection_policy": manifest["selection_policy"],
            }
        )
    return result
