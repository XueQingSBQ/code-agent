"""Separate task-input completeness from verification guidance in a 2x2 study.

Default mode audits old patches in fresh checkouts without model calls. --live
also probes the configured endpoint and runs an equal-budget factorial study.
Public discussion is an assisted diagnostic track, not standard SWE-bench input.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.prompts import DEFAULT_SYSTEM_PROMPT
from evals.context_efficiency.provider_health import probe
from evals.context_efficiency.repository_ab import (
    assessed_record,
    failed_worker_record,
    implementation_fingerprint,
    instance_from_record,
    service_unavailable,
    task_prompt,
)
from evals.context_efficiency.repository_environment import command, grade, save_json

CASES = ("pallets__flask-4992", "sympy__sympy-18087")
BEFORE_PROMPT = Path(__file__).with_name("fixtures") / "system_prompt_before_verification.txt"
HARNESS_PATH = Path(__file__).resolve().parents[1] / "context_efficiency" / "repository_ab.py"
ARMS = {
    "problem_only_before": (False, "before"),
    "problem_only_acceptance": (False, "acceptance"),
    "discussion_before": (True, "before"),
    "discussion_acceptance": (True, "acceptance"),
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_matrix(selection: dict[str, Any], repetitions: int) -> list[dict[str, Any]]:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    by_id = {task["instance_id"]: task for task in selection["selected"]}
    prompts = {
        "before": BEFORE_PROMPT.read_text(encoding="utf-8"),
        "acceptance": DEFAULT_SYSTEM_PROMPT,
    }
    randomizer = random.Random(20261026)
    specs = []
    for identifier in CASES:
        task = by_id[identifier]
        if not task["hints_text"].strip():
            raise ValueError(f"No public discussion for diagnostic case {identifier}")
        for repeat in range(1, repetitions + 1):
            order = list(ARMS)
            randomizer.shuffle(order)
            for arm in order:
                hints, workflow = ARMS[arm]
                specs.append(
                    {
                        "instance": task,
                        "variant": "production",
                        "study_arm": arm,
                        "repeat": repeat,
                        "max_turns": 40,
                        "timeout_seconds": 420,
                        "enable_budget_notices": True,
                        "deadline_hints": True,
                        "include_public_hints": hints,
                        "system_prompt": prompts[workflow],
                    }
                )
    return specs


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Never pool input tracks, partial passes, or unattempted cells."""
    summary = {}
    for arm in ARMS:
        rows = [row for row in records if row["study_arm"] == arm]
        summary[arm] = {
            "attempts": len(rows),
            "strict_successes": sum(bool(row["success"]) for row in rows),
            "strict_success_rate": sum(bool(row["success"]) for row in rows) / len(rows)
            if rows
            else None,
            "patches_passing_independent_tests": sum(bool(row["tests_passed"]) for row in rows),
            "exit_reasons": dict(Counter(row.get("exit_reason") for row in rows)),
        }
    return summary


async def audit_run(source: Path, destination: Path) -> dict[str, Any]:
    """Regrade the frozen production patch, preserving original task status."""
    if await asyncio.to_thread(destination.exists):
        raise FileExistsError("Audit destination must be fresh")
    spec = json.loads(await asyncio.to_thread((source / "spec.json").read_text, encoding="utf-8"))
    original = await asyncio.to_thread(assessed_record, source)
    patch = await asyncio.to_thread((source / "submission.patch").read_text, encoding="utf-8")
    prompt = await asyncio.to_thread((source / "prompt.txt").read_text, encoding="utf-8")
    instance = instance_from_record(spec["instance"])
    validation = await grade(
        instance,
        Path(spec["instance"]["cache"]),
        Path(spec["instance"]["python"]),
        destination / "grading",
        patch,
    )
    record = {
        "source": str(source),
        "instance_id": instance.instance_id,
        "patch_sha256": digest(patch),
        "original_success": original["success"],
        "original_exit_reason": original["exit_reason"],
        "original_validation_success": original["tests_passed"],
        "regraded_validation": validation,
        "public_discussion_chars": len(instance.hints_text),
        "public_discussion_in_original_prompt": bool(instance.hints_text.strip())
        and instance.hints_text in prompt,
        "original_prompt_sha256": digest(prompt),
    }
    save_json(destination / "audit.json", record)
    return record


async def run(
    selection_path: Path,
    output: Path,
    *,
    repetitions: int = 2,
    live: bool = False,
    audit_sources: tuple[Path, ...] = (),
) -> dict[str, Any]:
    if await asyncio.to_thread(output.exists):
        raise FileExistsError("Use a fresh output directory; prior evidence is immutable")
    selection = json.loads(await asyncio.to_thread(selection_path.read_text, encoding="utf-8"))
    specs = await asyncio.to_thread(build_matrix, selection, repetitions)
    for spec in specs:
        for key in ("cache", "python"):
            if not await asyncio.to_thread(Path(spec["instance"][key]).exists):
                raise FileNotFoundError(spec["instance"][key])
    fingerprint = await asyncio.to_thread(implementation_fingerprint)
    harness_hash = hashlib.sha256(await asyncio.to_thread(HARNESS_PATH.read_bytes)).hexdigest()
    study_hash = digest(await asyncio.to_thread(Path(__file__).read_text, encoding="utf-8"))
    for spec in specs:
        spec["expected_runtime_sha256"] = fingerprint
        spec["expected_harness_sha256"] = harness_hash
        spec["expected_system_prompt_sha256"] = digest(spec["system_prompt"])
        instance = instance_from_record(spec["instance"])
        spec["expected_prompt_sha256"] = digest(
            task_prompt(
                instance,
                Path(spec["instance"]["python"]),
                include_public_hints=spec["include_public_hints"],
            )
        )
        directory = output / "runs" / instance.instance_id / str(spec["repeat"]) / spec["study_arm"]
        save_json(directory / "spec.json", spec)
    save_json(
        output / "protocol.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "cases": CASES,
            "repetitions": repetitions,
            "parallel_cases": 2,
            "runtime_sha256": fingerprint,
            "harness_sha256": harness_hash,
            "study_sha256": study_hash,
            "arms": ARMS,
            "order": [(s["instance"]["instance_id"], s["repeat"], s["study_arm"]) for s in specs],
            "selection": "Purposive diagnostics of two prior semantic failures; not a general coding score",
            "inputs": "Problem-only and public-discussion-assisted tracks remain separate. No reference/test patch is sent to the model.",
            "grading": "Unchanged clean-checkout hidden regressions; strict completed + passing patch + no execution error",
            "compression": "200K production in every arm; no compression efficacy claim",
        },
    )
    records: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "status": "prepared",
        "planned": len(specs),
        "attempted": 0,
        "records": records,
        "summary": summarize(records),
        "audits": [],
    }

    def persist() -> None:
        report.update(attempted=len(records), summary=summarize(records))
        save_json(output / "report.json", report)

    persist()
    for index, source in enumerate(audit_sources):
        report["audits"].append(await audit_run(source, output / "audits" / str(index)))
        persist()
    if not live:
        report["status"] = "offline_complete_live_not_run"
        persist()
        return report
    health = await probe()
    save_json(output / "endpoint-probe.json", health)
    if not health["available"]:
        report["status"] = "endpoint_unavailable"
        persist()
        return report
    report["status"] = "running"
    persist()
    stop = asyncio.Event()

    async def paired(identifier: str) -> None:
        for spec in specs:
            if spec["instance"]["instance_id"] != identifier:
                continue
            if stop.is_set():
                return
            if implementation_fingerprint() != fingerprint:
                raise RuntimeError("Runtime changed during the study")
            directory = output / "runs" / identifier / str(spec["repeat"]) / spec["study_arm"]
            print(f"START {identifier}/{spec['repeat']}/{spec['study_arm']}", flush=True)
            result = await command(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "evals.context_efficiency.repository_ab",
                    "--worker",
                    str(directory / "spec.json"),
                ],
                cwd=Path.cwd(),
                timeout_seconds=spec["timeout_seconds"] + 360,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            (directory / "worker.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if not (directory / "result.json").exists():
                save_json(
                    directory / "result.json",
                    failed_worker_record(
                        spec,
                        directory,
                        f"Worker exit={result.returncode}, timeout={result.timed_out}: {result.stderr[-1500:]}",
                    ),
                )
            row = assessed_record(directory)
            row["study_arm"] = spec["study_arm"]
            records.append(row)
            persist()
            if service_unavailable(row):
                stop.set()

    try:
        await asyncio.gather(*(paired(identifier) for identifier in CASES))
        if implementation_fingerprint() != fingerprint:
            raise RuntimeError("Runtime changed before study completion")
    finally:
        report["status"] = "complete" if len(records) == len(specs) else "incomplete"
        persist()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--audit", type=Path, action="append", default=[])
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(
        run(
            args.selection.resolve(),
            args.output.resolve(),
            repetitions=args.repetitions,
            live=args.live,
            audit_sources=tuple(path.resolve() for path in args.audit),
        )
    )
    print(
        json.dumps(
            {key: report[key] for key in ("status", "planned", "attempted", "summary")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
