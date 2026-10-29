"""Diagnostic comparison of budget notices vs a separately enlarged budget.

Cases are intentionally selected from prior budget failures, not a representative
benchmark. Control and guided arms have identical 40-call / 420-second limits;
the 80-call / 840-second arm diagnoses budget size and is never pooled with them.
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

from evals.context_efficiency.repository_ab import (
    assessed_record,
    failed_worker_record,
    implementation_fingerprint,
    service_unavailable,
)
from evals.context_efficiency.repository_environment import command, save_json

CASES = ("sympy__sympy-17022", "sympy__sympy-18087", "sympy__sympy-18532")
ARMS: dict[str, dict[str, Any]] = {
    "control": {"max_turns": 40, "timeout_seconds": 420, "enable_budget_notices": False},
    "guided": {"max_turns": 40, "timeout_seconds": 420, "enable_budget_notices": True},
    "extended": {"max_turns": 80, "timeout_seconds": 840, "enable_budget_notices": False},
}


def diagnose(record: dict[str, Any]) -> dict[str, Any]:
    """Extract observed behavior, without classifying every tool error as a bug."""
    path = Path(record["artifact_directory"]) / "events.jsonl"
    events = (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )
    calls = [event["data"] for event in events if event["type"] == "tool_use"]
    completed = [event["data"] for event in events if event["type"] == "tool_complete"]
    signatures = Counter(
        (call["name"], json.dumps(call["input"], sort_keys=True)) for call in calls
    )
    finished_ids = {item["tool_id"] for item in completed}
    return {
        "instance_id": record["instance_id"],
        "variant": record["variant"],
        "exit_reason": record.get("exit_reason"),
        "errors": record.get("errors", []),
        "tests_passed": record.get("tests_passed", False),
        "success": record["success"],
        "model_calls": len(record.get("requests", [])),
        "agent_seconds": record.get("agent_seconds"),
        "patch_passed_without_clean_completion": bool(
            record.get("tests_passed") and not record["success"]
        ),
        "tool_counts": dict(Counter(call["name"] for call in calls)),
        "observed_tool_errors": sum(bool(item["is_error"]) for item in completed),
        "no_op_edits": sum(
            call["name"] == "edit"
            and call["input"].get("old_text") == call["input"].get("new_text")
            for call in calls
        ),
        "identical_repeated_calls": [
            {"tool": name, "arguments": json.loads(arguments), "count": count}
            for (name, arguments), count in signatures.most_common()
            if count > 1
        ],
        "last_calls": calls[-6:],
        "calls_without_completion_event": [call for call in calls if call["id"] not in finished_ids],
        "budget_notices": sum(event["type"] == "budget_warning" for event in events),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for arm in ARMS:
        rows = [record for record in records if record["study_arm"] == arm]
        summary[arm] = {
            "attempts": len(rows),
            "expected": len(CASES),
            "strict_successes": sum(bool(row["success"]) for row in rows),
            "patches_passing_independent_tests": sum(bool(row["tests_passed"]) for row in rows),
            "exit_reasons": dict(
                Counter(row.get("exit_reason") or "timeout_or_error" for row in rows)
            ),
            "model_calls": sum(len(row.get("requests", [])) for row in rows),
            "usage_complete_attempts": sum(bool(row["usage"]["complete"]) for row in rows),
        }
    return summary


async def run(selection_path: Path, baseline_path: Path, output: Path) -> None:
    if await asyncio.to_thread(output.exists):
        raise FileExistsError("Use a fresh output directory; baseline evidence is immutable")
    selection = json.loads(await asyncio.to_thread(selection_path.read_text, encoding="utf-8"))
    by_id = {item["instance_id"]: item for item in selection["selected"]}
    tasks = [by_id[identifier] for identifier in CASES]
    for task in tasks:
        for key in ("cache", "python"):
            if not await asyncio.to_thread(Path(task[key]).exists):
                raise FileNotFoundError(f"Missing {key} for {task['instance_id']}")
    baseline = json.loads(await asyncio.to_thread(baseline_path.read_text, encoding="utf-8"))
    save_json(
        output / "baseline-diagnostics.json", [diagnose(record) for record in baseline["records"]]
    )
    fingerprint = implementation_fingerprint()
    randomizer = random.Random(20261025)
    orders = {}
    for task in tasks:
        order = list(ARMS)
        randomizer.shuffle(order)
        orders[task["instance_id"]] = order
    save_json(
        output / "protocol.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "cases": CASES,
            "arms": ARMS,
            "arm_order": orders,
            "parallel_cases": 3,
            "repetitions": 1,
            "runtime_sha256": fingerprint,
            "study_sha256": hashlib.sha256(
                await asyncio.to_thread(Path(__file__).read_bytes)
            ).hexdigest(),
            "selection": "Purposive diagnostics of prior max-turns/test-passing and timeout failures; not a representative score",
            "compression": "production 200K in every arm; this is not a compression experiment",
            "grading": "same clean-checkout independent regressions; strict completion required",
            "baseline_report": str(baseline_path),
        },
    )
    records: list[dict[str, Any]] = []
    stop = asyncio.Event()

    def report() -> None:
        save_json(
            output / "report.json",
            {
                "status": "complete" if len(records) == len(CASES) * len(ARMS) else "incomplete",
                "planned": len(CASES) * len(ARMS),
                "completed": len(records),
                "summary": summarize(records),
                "records": records,
            },
        )

    async def paired(task: dict[str, Any]) -> None:
        for arm in orders[task["instance_id"]]:
            if stop.is_set():
                return
            if implementation_fingerprint() != fingerprint:
                raise RuntimeError("Runtime changed during evaluation; do not mix implementations")
            directory = output / "runs" / task["instance_id"] / arm
            spec = {
                "instance": task,
                "variant": "production",
                "repeat": 1,
                "deadline_hints": True,
                **ARMS[arm],
            }
            save_json(directory / "spec.json", spec)
            print(f"START {task['instance_id']} {arm}", flush=True)
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
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
                env=environment,
            )
            (directory / "worker.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if not (directory / "result.json").exists():
                save_json(
                    directory / "result.json",
                    failed_worker_record(
                        spec,
                        directory,
                        f"Worker returncode={result.returncode}, timed_out={result.timed_out}; {result.stderr[-1500:]}",
                    ),
                )
            record = assessed_record(directory)
            record["study_arm"] = arm
            record["diagnostics"] = diagnose(record)
            records.append(record)
            report()
            print(
                f"DONE {task['instance_id']} {arm}: success={record['success']} exit={record['exit_reason']} calls={len(record.get('requests', []))}",
                flush=True,
            )
            if service_unavailable(record):
                stop.set()

    report()
    await asyncio.gather(*(paired(task) for task in tasks))
    if implementation_fingerprint() != fingerprint:
        raise RuntimeError("Runtime changed before evaluation completed")
    report()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.selection.resolve(), args.baseline.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
