"""Aggregate observed evidence without dropping failed or unfinished attempts."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from evals.context_efficiency.repository_environment import save_json


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def nearest_rank(values: list[float], quantile: float) -> float | None:
    return sorted(values)[math.ceil(len(values) * quantile) - 1] if values else None


def durable_worker_states(root: Path, concurrency: int) -> dict[str, Any]:
    """Read partial progress even when a wave timed out before gather returned."""
    database = root / "worker-pilot" / f"concurrency-{concurrency}" / "repo/.carbide/sessions.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        runs = connection.execute("SELECT status FROM agent_runs").fetchall()
        tasks = connection.execute("SELECT status FROM todo_items").fetchall()
    return {
        "worker_statuses": dict(Counter(row[0] for row in runs)),
        "todo_statuses": dict(Counter(row[0] for row in tasks)),
    }


def collect(root: Path) -> dict[str, Any]:
    reliability = read(root / "reliability-final" / "results.json")
    trials = reliability["records"]
    repetitions = reliability["environment"]["repetitions"]
    if len(trials) != repetitions * len(reliability["environment"]["scenarios"]):
        raise ValueError("Incomplete reliability run")
    real = read(root / "real-issues" / "report.json")
    workers = read(root / "worker-pilot" / "results.json")
    if real["status"] != "complete" or len(workers) != 2:
        raise ValueError("Live experiments have not finished; no final report may be emitted")
    offline = reliability["summary"]
    for name, row in offline.items():
        records = [r for r in trials if r["scenario"] == name]
        resumes = [
            r["details"]["resume_ms"] for r in records if "resume_ms" in r.get("details", {})
        ]
        row["resume_p95_ms"] = nearest_rank(resumes, 0.95)
    contention = [r for r in trials if r["scenario"] == "eight_process_exclusive_claim"]
    live: dict[str, Any] = {}
    for variant in real["config"]["variants"]:
        rows = [r for r in real["records"] if r["variant"] == variant]
        tools = sum(sum(r["tool_counts"].values()) for r in rows)
        errors = sum(r["tool_errors"] for r in rows)
        live[variant] = {
            "tasks": len(rows),
            "passed": sum(r["success"] for r in rows),
            "failure_categories": dict(Counter(r["failure_category"] for r in rows)),
            "requests": sum(r["usage"]["requests"] for r in rows),
            "prompt_tokens": sum(r["usage"]["prompt_tokens"] for r in rows),
            "tool_calls": tools,
            "tool_errors": errors,
            "tool_error_observation_ratio": errors / tools if tools else None,
            "usage_complete_runs": sum(r["usage"]["complete"] for r in rows),
            "pairing_violations": sum(r["pairing_violations"] for r in rows),
            "compression_runs": sum(bool(r["layers"]) for r in rows),
        }
    return {
        "scope": "one-machine evidence, not a production SLA or official SWE-bench score",
        "reliability": offline,
        "contention": {
            "rounds": len(contention),
            "claim_attempts": sum(r.get("details", {}).get("contenders", 0) for r in contention),
            "exclusive_rounds_passed": sum(r["passed"] for r in contention),
        },
        "real_issues": live,
        "worker_pilot": [
            {
                key: r[key]
                for key in (
                    "concurrency",
                    "success",
                    "seconds",
                    "usage",
                    "peak_overlapping_model_requests",
                    "pairing_violations",
                    "errors",
                )
            }
            | {
            **durable_worker_states(root, r["concurrency"]),
                "independent_tests_passed": sum(
                    status == "passed" for status in r["validation"]["fail_to_pass"].values()
                ),
                "independent_tests_total": len(r["validation"]["fail_to_pass"]),
            }
            for r in workers
        ],
        "task_results": [
            {
                key: r[key]
                for key in (
                    "instance_id",
                    "variant",
                    "success",
                    "tests_passed",
                    "exit_reason",
                    "failure_category",
                    "layers",
                    "agent_seconds",
                )
            }
            for r in real["records"]
        ],
        "limitations": [
            "Runtime fault injection uses deterministic providers, not real model recovery quality",
            "20 repeated fault trials are not 20 independent real user workloads",
            "Real issues: 6 fixed tasks, one attempt per arm; small native subset, not official leaderboard",
            "Worker pilot: 1 derived four-issue maintenance workload; fixed arm order and 420s wall budget",
            "Worker concurrency 1 vs 2 does not compare natural single-Agent vs full multi-Agent products",
            "No compression benefit may be claimed from runs that do not trigger compression",
            "Token counts include cache reads/writes and do not imply a billing cost reduction",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.root.resolve())
    save_json(args.root / "evidence.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
