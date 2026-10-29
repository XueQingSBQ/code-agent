"""Combine compatible real issues into one autonomous maintenance task.

Issues are transplanted to one pinned SymPy checkout. Every accepted combination
must reproduce failures and pass its independent reference repair before any
model runs. This workload is reported separately from the original issue subset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evals.context_efficiency.repository_ab import Config, instance_from_record, run
from evals.context_efficiency.repository_environment import (
    apply_patch_file,
    checked,
    checkout,
    command,
    grade,
    production_path,
    save_json,
)
from evals.swebench.instance import SWEBenchInstance


def load_sequence_manifest(path: Path, available_ids: set[str]) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    tasks = manifest.get("tasks")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("selection_policy"), str)
        or not isinstance(tasks, list)
        or not tasks
    ):
        raise ValueError(
            "Sequence manifest must use schema_version=1, define selection_policy and contain tasks"
        )
    seen: set[str] = set()
    previous_stage = 0
    for task in tasks:
        instance_id = task.get("instance_id") if isinstance(task, dict) else None
        stage = task.get("stage") if isinstance(task, dict) else None
        if not isinstance(instance_id, str) or not isinstance(stage, int):
            raise ValueError("Every sequence task must contain string instance_id and integer stage")
        if instance_id in seen:
            raise ValueError(f"Duplicate sequence task: {instance_id}")
        if stage <= previous_stage:
            raise ValueError("Sequence stages must be strictly increasing")
        if instance_id not in available_ids:
            raise ValueError(f"Sequence task is absent from the source selection: {instance_id}")
        seen.add(instance_id)
        previous_stage = stage
    return manifest


async def combined_patch(workspace: Path, base_ref: str, task: SWEBenchInstance) -> tuple[str, str]:
    names = (await checked(["git", "diff", "--name-only", base_ref], cwd=workspace)).splitlines()
    sources = [name for name in names if production_path(task, name)]
    tests = [name for name in names if "tests" in Path(name).parts]
    source = await command(["git", "diff", "--binary", base_ref, "--", *sources], cwd=workspace)
    test = await command(["git", "diff", "--binary", base_ref, "--", *tests], cwd=workspace)
    if source.returncode or test.returncode or not sources or not tests:
        raise ValueError("Incomplete composite patch")
    return source.stdout, test.stdout


def composite_instance(
    base: SWEBenchInstance,
    accepted: list[SWEBenchInstance],
    source: str,
    tests: str,
) -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id=f"sympy-maintenance-{len(accepted)}-issues",
        repo=base.repo,
        base_commit=base.base_commit,
        problem_statement="Repair all of the following reported problems in this maintenance checkout. "
        "Preserve existing public behavior and verify each repair.\n\n"
        + "\n\n".join(f"Issue {task.instance_id}:\n{task.problem_statement}" for task in accepted),
        hints_text="",
        test_patch=tests,
        patch=source,
        version=base.version,
        environment_setup_commit=base.base_commit,
        FAIL_TO_PASS=list(dict.fromkeys(name for task in accepted for name in task.FAIL_TO_PASS)),
        PASS_TO_PASS=list(dict.fromkeys(name for task in accepted for name in task.PASS_TO_PASS)),
    )


async def prepare_session(
    source_root: Path,
    output: Path,
    issue_count: int = 6,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    selection = json.loads((source_root / "selection.json").read_text(encoding="utf-8"))
    candidates = [item for item in selection["selected"] if item["repo"] == "sympy/sympy"]
    manifest = (
        load_sequence_manifest(manifest_path, {item["instance_id"] for item in candidates})
        if manifest_path is not None
        else None
    )
    if manifest is not None:
        order = [task["instance_id"] for task in manifest["tasks"]]
        by_id = {item["instance_id"]: item for item in candidates}
        candidates = [by_id[instance_id] for instance_id in order]
        issue_count = len(candidates)
    base_record = candidates[0]
    base = instance_from_record(base_record)
    base_cache = Path(base_record["cache"])
    base_ref = await checked(["git", "rev-parse", "HEAD"], cwd=base_cache)
    previous = base_cache
    python = Path(base_record["python"])
    accepted: list[SWEBenchInstance] = []
    checks: list[dict[str, Any]] = []
    final: SWEBenchInstance | None = None
    for item in candidates:
        if len(accepted) >= issue_count:
            break
        task = instance_from_record(item)
        directory = output / "compatibility" / task.instance_id
        workspace = directory / "repo"
        record_path = directory / "result.json"
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            checks.append(record)
            if record["eligible"]:
                accepted.append(task)
                previous = workspace
                final = composite_instance(
                    base,
                    accepted,
                    (directory / "reference" / "source.patch").read_text(encoding="utf-8"),
                    (directory / "reference" / "test.patch").read_text(encoding="utf-8"),
                )
            print(
                f"SESSION CACHED {task.instance_id} accepted={len(accepted)}/{issue_count}",
                flush=True,
            )
            continue
        directory.mkdir(parents=True, exist_ok=True)
        await checkout(previous, base, workspace)
        try:
            await apply_patch_file(workspace, task.patch, directory / "source.patch")
            await apply_patch_file(workspace, task.test_patch, directory / "test.patch")
            await checked(["git", "add", "--force", "."], cwd=workspace)
            source_patch, test_patch = await combined_patch(workspace, base_ref, base)
            candidate = composite_instance(base, [*accepted, task], source_patch, test_patch)
            baseline = await grade(candidate, base_cache, python, directory / "baseline")
            reference = await grade(
                candidate, base_cache, python, directory / "reference", gold=True
            )
            eligible = (
                reference["success"]
                and any(value == "failed" for value in baseline["fail_to_pass"].values())
                and all(value == "passed" for value in baseline["pass_to_pass"].values())
            )
            record = {
                "instance_id": task.instance_id,
                "eligible": eligible,
                "baseline": baseline,
                "reference": reference,
            }
            if eligible:
                await checked(
                    [
                        "git",
                        "-c",
                        "user.name=Evaluation",
                        "-c",
                        "user.email=eval@example.invalid",
                        "commit",
                        "-m",
                        f"Reference {task.instance_id}",
                    ],
                    cwd=workspace,
                )
                previous = workspace
                accepted.append(task)
                final = candidate
        except Exception as exc:
            record = {
                "instance_id": task.instance_id,
                "eligible": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        checks.append(record)
        save_json(directory / "result.json", record)
        print(
            f"SESSION PREFLIGHT {task.instance_id} eligible={record['eligible']} accepted={len(accepted)}/{issue_count}",
            flush=True,
        )
    if final is None or len(accepted) < issue_count:
        raise RuntimeError(
            f"Only {len(accepted)} compatible issues; compatibility artifacts retained"
        )
    result = {
        "requested": 1,
        "kind": "one autonomous task combining compatible real issue transplants",
        "original_issue_ids": [task.instance_id for task in accepted],
        "checks": checks,
        "selected": [{**asdict(final), "python": str(python), "cache": str(base_cache)}],
    }
    if manifest is not None:
        result.update(
            {
                "manifest": manifest,
                "manifest_path": str(manifest_path),
                "selection_policy": manifest["selection_policy"],
            }
        )
    save_json(output / "selection.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--issues", type=int, default=6)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if not (output / "selection.json").exists():
        asyncio.run(
            prepare_session(
                args.source.resolve(),
                output,
                args.issues,
                args.manifest.resolve() if args.manifest else None,
            )
        )
    if not args.prepare_only:
        asyncio.run(
            run(
                output,
                Config(
                    repetitions=3,
                    parallel=args.parallel,
                    cases=1,
                    max_turns=150,
                    timeout_seconds=1800,
                ),
            )
        )


if __name__ == "__main__":
    main()
