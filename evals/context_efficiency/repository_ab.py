"""Autonomous, paired coding evaluation on frozen real GitHub issues.

Run ``python -m evals.context_efficiency.repository_ab --output ...``. Each
attempt owns a separate process, checkout and real Carbide tools. Production
200K and accelerated 32K compression profiles are reported separately.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.context.snip import SnipCompressor
from carbide.providers.base import Provider
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry
from carbide.tools.snip import SnipTool
from evals.context_efficiency.layered_ab import MeteredProvider, Request, usage_totals
from evals.context_efficiency.repository_environment import (
    checked,
    checkout,
    command,
    grade,
    prepare,
    production_path,
    save_json,
    source_root,
)
from evals.swebench.instance import SWEBenchInstance

VARIANTS = {"off": None, "production": 200_000, "pressure": 32_000}


@dataclass(frozen=True)
class Config:
    repetitions: int = 3
    parallel: int = 6
    cases: int = 20
    variants: tuple[str, ...] = ("off", "production", "pressure")
    max_turns: int = 60
    timeout_seconds: int = 600

    def __post_init__(self) -> None:
        if (
            min(self.repetitions, self.parallel, self.cases, self.max_turns, self.timeout_seconds)
            < 1
        ):
            raise ValueError("Evaluation bounds must be positive")
        if "off" not in self.variants or not set(self.variants).issubset(VARIANTS):
            raise ValueError("Select off and at least one known compression profile")
        if len(set(self.variants)) != len(self.variants) or len(self.variants) < 2:
            raise ValueError("Variants must be distinct and include an experimental profile")


def instance_from_record(record: dict[str, Any]) -> SWEBenchInstance:
    return SWEBenchInstance(
        **{field: record[field] for field in SWEBenchInstance.__dataclass_fields__}
    )


class TraceProvider(MeteredProvider):
    def __init__(self, inner: Provider, directory: Path, max_requests: int) -> None:
        super().__init__(inner)
        self.directory = directory
        self.max_requests = max_requests

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        if len(self.requests) >= self.max_requests:
            raise RuntimeError("Evaluation request ceiling reached")
        index = len(self.requests)
        await asyncio.to_thread(
            save_json,
            self.directory / f"{index:03d}.json",
            {"messages": messages, "system": system, "tools": tools, "max_tokens": max_tokens},
        )
        try:
            async for chunk in super().stream(
                messages, system, tools, max_tokens, temperature, **kwargs
            ):
                yield chunk
        finally:
            if len(self.requests) > index:
                await asyncio.to_thread(
                    save_json,
                    self.directory / f"{index:03d}-metrics.json",
                    asdict(self.requests[index]),
                )


def task_prompt(
    instance: SWEBenchInstance, python: Path, *, include_public_hints: bool = False
) -> str:
    executable = python.as_posix()
    discussion = (
        "\nPublic issue discussion (additional diagnostic input; not a reference patch):\n"
        + instance.hints_text
        + "\n"
        if include_public_hints and instance.hints_text.strip()
        else ""
    )
    return f"""Fix this real issue in {instance.repo}. The checkout is at the reported base commit.

{instance.problem_statement}{discussion}

Investigate the repository, reproduce the problem, implement a maintainable fix, and run
relevant existing and new regression tests. Work autonomously until the fix is verified.
Use Todo for multi-step work when useful. Explain the final change and test results.

The test Python and dependencies are already installed. In bash use
\"{executable}\" -m pytest -q -o addopts= <relevant test file>.
For Flask use PYTHONPATH=src before that command. Do not install dependencies or access
the network. Modify this checkout only. Preserve existing public behavior, tests and
test configuration. Do not read parent directories or external evaluation artifacts.
"""


async def task_events(
    agent: Agent,
    prompts: list[str],
    phases: list[dict[str, Any]],
    *,
    deadline: float | None = None,
) -> AsyncIterator[Any]:
    """Continue one conversation, stopping follow-ups after an execution failure."""
    for index, prompt in enumerate(prompts, start=1):
        phase: dict[str, Any] = {
            "index": index,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "exit_reason": None,
        }
        phases.append(phase)
        async for event in agent.query(prompt, deadline=deadline):
            if event.type == "turn_complete":
                phase["exit_reason"] = event.data.get("reason")
            yield event
        if phase["exit_reason"] != "completed":
            break


def verify_frozen_inputs(spec: dict[str, Any]) -> None:
    """Reject protocol drift before model work and before scoring a result."""
    instance = instance_from_record(spec["instance"])
    prompt = task_prompt(
        instance,
        Path(spec["instance"]["python"]),
        include_public_hints=spec.get("include_public_hints", False),
    )
    actual = {
        "runtime": implementation_fingerprint(),
        "harness": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prompt": hashlib.sha256(prompt.encode()).hexdigest(),
        "system_prompt": hashlib.sha256(spec.get("system_prompt", "").encode()).hexdigest(),
    }
    for name, value in actual.items():
        expected = spec.get(f"expected_{name}_sha256")
        if expected is not None and expected != value:
            raise ValueError(f"Frozen {name} changed; refuse mixed-protocol evaluation")


async def source_patch(instance: SWEBenchInstance, workspace: Path) -> tuple[str, list[str]]:
    root = source_root(instance)
    # This operates only in a newly-created disposable evaluation checkout.
    await checked(["git", "add", "--intent-to-add", "--", root], cwd=workspace)
    names = (await checked(["git", "diff", "--name-only", "--", root], cwd=workspace)).splitlines()
    files = [name for name in names if production_path(instance, name)]
    if not files:
        return "", files
    result = await command(["git", "diff", "--binary", "--", *files], cwd=workspace)
    if result.returncode:
        raise RuntimeError(f"Cannot capture patch: {result.stderr}")
    # A trailing space is a valid empty context line in a unified diff.
    # strip() corrupts the final hunk when it ends with that line.
    return result.stdout, files


def repository_usage(requests: list[Request]) -> dict[str, Any]:
    total = usage_totals(requests)
    invalid = sum(
        request.usage is not None
        and (
            sum(
                request.usage.get(key, 0)
                for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
            )
            <= 0
            or any(value < 0 for value in request.usage.values())
        )
        for request in requests
    )
    total["invalid_usage_requests"] = invalid
    total["complete"] = bool(requests) and total["complete"] and not invalid
    return total


def failure_category(record: dict[str, Any]) -> str:
    if record["success"]:
        return "passed"
    if record.get("exit_reason") == "provider_error":
        return "provider_error"
    if record.get("usage", {}).get("invalid_usage_requests", 0):
        return "invalid_provider_usage"
    if record.get("exit_reason") in {"worker_failure", "interrupted"}:
        return "infrastructure_failure"
    if record.get("errors") or record.get("exit_reason") != "completed":
        return "runtime_failure"
    if record.get("validation", {}).get("error"):
        return "grading_error"
    return "test_failure"


def service_unavailable(record: dict[str, Any]) -> bool:
    errors = " ".join(record.get("errors", [])).lower()
    return (
        "no available accounts" in errors
        or "no available channel" in errors
        or "overloaded_error" in errors
        or bool(record.get("usage", {}).get("invalid_usage_requests", 0))
    )


def assessed_record(directory: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if record.get("requests") and all("usage" in r for r in record["requests"]):
        record["usage"] = repository_usage([Request(**r) for r in record["requests"]])
    correction = directory / "assessment.json"
    if correction.exists():
        record.update(json.loads(correction.read_text(encoding="utf-8"))["correction"])
    # A nonempty model request cannot have zero total input. Preserve the raw
    # exit reason but do not score an invalid stream as a clean completion.
    if record["usage"].get("invalid_usage_requests", 0):
        record["success"] = False
    record["failure_category"] = failure_category(record)
    return record


async def attempt(spec_path: Path) -> dict[str, Any]:
    spec = json.loads(await asyncio.to_thread(spec_path.read_text, encoding="utf-8"))
    instance = instance_from_record(spec["instance"])
    directory = spec_path.parent
    workspace = directory / "repo"
    python = Path(spec["instance"]["python"])
    cache = Path(spec["instance"]["cache"])
    started = time.perf_counter()
    provider: TraceProvider | None = None
    errors: list[str] = []
    compressions: list[dict[str, Any]] = []
    tool_counts: Counter[str] = Counter()
    tool_errors = 0
    exit_reason: str | None = None
    phases: list[dict[str, Any]] = []
    followups = spec.get("followup_prompts")
    patch = ""
    files: list[str] = []
    validation: dict[str, Any] = {"success": False, "error": "Agent did not run"}
    from carbide.providers.anthropic import create_anthropic_provider

    try:
        # Old specs have no expected hashes and retain their existing protocol.
        if any(key.startswith("expected_") for key in spec):
            verify_frozen_inputs(spec)
        await checkout(cache, instance, workspace)
        registry = create_default_registry()
        if spec["variant"] == "off":
            # Identical tool schema; this queue never mutates baseline history.
            registry.register(SnipTool(SnipCompressor()))
        provider = TraceProvider(
            create_anthropic_provider(),
            directory / "requests",
            spec["max_turns"] * (len(followups) if followups else 1) + 30,
        )
        agent = Agent(
            provider=provider,
            project_dir=workspace,
            tool_registry=registry,
            enable_compression=spec["variant"] != "off",
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_subagents=False,
            enable_todo_tools=True,
            enable_governance=True,
            enable_session_store=True,
            close_provider=False,
            approval_callback=lambda _call, _prompt: True,
            max_turns=spec["max_turns"],
            max_output_tokens=4096,
            enable_budget_notices=spec.get("enable_budget_notices", True),
            system_prompt=spec.get("system_prompt"),
        )
        if agent.compression_manager is not None:
            agent.compression_manager.budget.max_tokens = VARIANTS[spec["variant"]] or 200_000
        # Keep the same environment guidance for both arms and the production
        # system prompt. No prescribed read order or artificial large payloads.
        prompt = task_prompt(
            instance, python, include_public_hints=spec.get("include_public_hints", False)
        )
        prompts = followups or [prompt]
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        if followups:
            save_json(directory / "followup-prompts.json", followups)
        events_path = directory / "events.jsonl"
        async with agent:
            async with asyncio.timeout(spec["timeout_seconds"]) as timeout_scope:
                with events_path.open("w", encoding="utf-8") as event_file:
                    async for event in task_events(
                        agent,
                        prompts,
                        phases,
                        deadline=timeout_scope.when()
                        if spec.get("deadline_hints", False)
                        else None,
                    ):
                        data = (
                            asdict(event.data)
                            if hasattr(event.data, "__dataclass_fields__")
                            else event.data
                        )
                        if event.type == "chunk":
                            continue
                        event_file.write(
                            json.dumps(
                                {"type": event.type, "data": data}, ensure_ascii=False, default=str
                            )
                            + "\n"
                        )
                        event_file.flush()
                        if event.type == "compression":
                            compressions.append(data)
                        elif event.type == "tool_use":
                            tool_counts[data["name"]] += 1
                            print(
                                f"TOOL {instance.instance_id}/{spec['variant']}/{spec['repeat']} {data['name']}",
                                flush=True,
                            )
                        elif event.type == "tool_complete":
                            tool_errors += int(bool(data.get("is_error", False)))
                        elif event.type == "turn_complete":
                            exit_reason = data.get("reason")
                        elif event.type == "error":
                            errors.append(str(data))
        if any(key.startswith("expected_") for key in spec):
            verify_frozen_inputs(spec)
        patch, files = await source_patch(instance, workspace)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        if workspace.exists():
            try:
                patch, files = await source_patch(instance, workspace)
            except Exception as patch_error:
                errors.append(f"Patch capture: {patch_error}")
    finally:
        if provider is not None:
            client = getattr(provider.inner, "client", None)
            if client is not None:
                await client.close()
    agent_seconds = time.perf_counter() - started
    (directory / "submission.patch").write_text(patch, encoding="utf-8")
    # Always grade partial patches, but keep bounded/runtime failures separate.
    validation = await grade(instance, cache, python, directory / "grading", patch)
    requests = provider.requests if provider is not None else []
    record = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "variant": spec["variant"],
        "repeat": spec["repeat"],
        "model": str(getattr(provider.inner, "model", "unknown")) if provider else "unknown",
        "success": bool(patch)
        and validation["success"]
        and exit_reason == "completed"
        and len(phases) == (len(followups) if followups else 1)
        and not errors,
        "tests_passed": validation["success"],
        "exit_reason": exit_reason,
        "phases": phases,
        "harness_sha256": hashlib.sha256(
            await asyncio.to_thread(Path(__file__).read_bytes)
        ).hexdigest(),
        "execution_budget": {
            "notices": spec.get("enable_budget_notices", True),
            "deadline_hints": spec.get("deadline_hints", False),
            "max_turns": spec["max_turns"],
            "timeout_seconds": spec["timeout_seconds"],
        },
        "task_input": {
            "include_public_hints": spec.get("include_public_hints", False),
            "prompt_sha256": phases[0]["prompt_sha256"] if phases else None,
            "system_prompt_sha256": hashlib.sha256(agent.system_prompt.encode()).hexdigest()
            if provider is not None and phases
            else None,
        },
        "errors": errors,
        "validation": validation,
        "changed_files": files,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "usage": repository_usage(requests),
        "requests": [asdict(item) for item in requests],
        "tool_counts": dict(tool_counts),
        "tool_errors": tool_errors,
        "compressions": compressions,
        "layers": sorted({layer for item in compressions for layer in item["layers"]}),
        "agent_seconds": agent_seconds,
        "total_seconds": time.perf_counter() - started,
        "peak_prompt_tokens": max(
            (
                (item.usage or {}).get("input_tokens", 0)
                + (item.usage or {}).get("cache_read_tokens", 0)
                + (item.usage or {}).get("cache_creation_tokens", 0)
                for item in requests
            ),
            default=0,
        ),
        "pairing_violations": sum(len(item.pairing_violations) for item in requests),
        "artifact_directory": str(directory),
    }
    if record["usage"]["invalid_usage_requests"]:
        record["success"] = False
    record["failure_category"] = failure_category(record)
    save_json(directory / "result.json", record)
    print(
        f"FINISHED {instance.instance_id}/{spec['variant']}/{spec['repeat']} success={record['success']} requests={len(requests)} input={record['usage']['prompt_tokens']} layers={record['layers']}",
        flush=True,
    )
    return record


def bootstrap_ci(values: list[float]) -> list[float] | None:
    if len(values) < 2:
        return None
    randomizer = random.Random(731)
    means = sorted(statistics.mean(randomizer.choices(values, k=len(values))) for _ in range(5000))
    return [means[124], means[4874]]


def failed_worker_record(spec: dict[str, Any], directory: Path, error: str) -> dict[str, Any]:
    """Keep unmeasured infrastructure failures in the denominator."""
    return {
        "instance_id": spec["instance"]["instance_id"],
        "repo": spec["instance"]["repo"],
        "base_commit": spec["instance"]["base_commit"],
        "variant": spec["variant"],
        "repeat": spec["repeat"],
        "model": "unknown",
        "success": False,
        "tests_passed": False,
        "exit_reason": "worker_failure",
        "errors": [error],
        "usage": {
            "prompt_tokens": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 0,
            "requests": 0,
            "complete": False,
        },
        "requests": [],
        "agent_seconds": float(spec["timeout_seconds"]),
        "total_seconds": float(spec["timeout_seconds"]),
        "peak_prompt_tokens": 0,
        "compressions": [],
        "layers": [],
        "tool_counts": {},
        "tool_errors": 0,
        "pairing_violations": 0,
        "changed_files": [],
        "validation": {"success": False, "error": error},
        "artifact_directory": str(directory),
    }


def implementation_fingerprint() -> str:
    source = Path(__file__).resolve().parents[2] / "src" / "carbide"
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*.py")):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["variant"]].append(record)
    summary: dict[str, Any] = {}
    baseline = {(item["instance_id"], item["repeat"]): item for item in groups.get("off", [])}
    for variant, runs in sorted(groups.items()):
        prompt = sum(item["usage"]["prompt_tokens"] for item in runs)
        pairs = [
            (baseline[(item["instance_id"], item["repeat"])], item)
            for item in runs
            if (item["instance_id"], item["repeat"]) in baseline
        ]
        complete_pairs = [
            (a, b)
            for a, b in pairs
            if a["usage"]["complete"] and b["usage"]["complete"] and a["usage"]["prompt_tokens"] > 0
        ]
        by_task: dict[str, list[float]] = defaultdict(list)
        for a, b in complete_pairs:
            by_task[b["instance_id"]].append(
                1 - b["usage"]["prompt_tokens"] / a["usage"]["prompt_tokens"]
            )
        task_means = [statistics.mean(values) for values in by_task.values()]
        both_successful = [(a, b) for a, b in complete_pairs if a["success"] and b["success"]]
        paired_baseline_tokens = sum(a["usage"]["prompt_tokens"] for a, _ in complete_pairs)
        paired_uncached_tokens = sum(a["usage"]["input_tokens"] for a, _ in complete_pairs)
        summaries = [
            request
            for item in runs
            for request in item["requests"]
            if request["purpose"] == "summary"
        ]
        summary[variant] = {
            "runs": len(runs),
            "unique_tasks": len({item["instance_id"] for item in runs}),
            "successful": sum(item["success"] for item in runs),
            "failure_categories": dict(Counter(failure_category(item) for item in runs)),
            "invalid_usage_requests": sum(
                item["usage"].get("invalid_usage_requests", 0) for item in runs
            ),
            "resolve_rate": sum(item["success"] for item in runs) / len(runs),
            "test_passing": sum(item["tests_passed"] for item in runs),
            "input_tokens": prompt,
            "uncached_input": sum(item["usage"]["input_tokens"] for item in runs),
            "cache_read_tokens": sum(item["usage"]["cache_read_tokens"] for item in runs),
            "cache_write_tokens": sum(item["usage"]["cache_creation_tokens"] for item in runs),
            "output_tokens": sum(item["usage"]["output_tokens"] for item in runs),
            "cache_hit_ratio": sum(item["usage"]["cache_read_tokens"] for item in runs) / prompt
            if prompt
            else 0,
            "requests": sum(item["usage"]["requests"] for item in runs),
            "summary_requests": sum(
                request["purpose"] == "summary" for item in runs for request in item["requests"]
            ),
            "summary_input_tokens": sum(
                (request.get("usage") or {}).get(key, 0)
                for request in summaries
                for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
            ),
            "peak_prompt_tokens": max(
                (item.get("peak_prompt_tokens", 0) for item in runs), default=0
            ),
            "compression_events": sum(len(item["compressions"]) for item in runs),
            "estimated_net_tokens_freed": sum(
                event["tokens_freed"] for item in runs for event in item["compressions"]
            ),
            "usage_complete_runs": sum(item["usage"]["complete"] for item in runs),
            "mean_agent_seconds": statistics.mean(item["agent_seconds"] for item in runs),
            "median_agent_seconds": statistics.median(item["agent_seconds"] for item in runs),
            "compression_runs": sum(bool(item["compressions"]) for item in runs),
            "layer_counts": dict(Counter(layer for item in runs for layer in item["layers"])),
            "pairing_violations": sum(item["pairing_violations"] for item in runs),
            "complete_pairs": len(complete_pairs),
            "triggered_complete_pairs": sum(bool(b["compressions"]) for _, b in complete_pairs),
            "both_successful_pairs": len(both_successful),
            "weighted_input_reduction": 1
            - sum(b["usage"]["prompt_tokens"] for _, b in complete_pairs) / paired_baseline_tokens
            if paired_baseline_tokens
            else None,
            "paired_uncached_input_reduction": 1
            - sum(b["usage"]["input_tokens"] for _, b in complete_pairs) / paired_uncached_tokens
            if paired_uncached_tokens
            else None,
            "mean_task_input_reduction": statistics.mean(task_means) if task_means else None,
            "task_bootstrap_95_ci": bootstrap_ci(task_means),
            "both_successful_input_reduction": 1
            - sum(b["usage"]["prompt_tokens"] for _, b in both_successful)
            / sum(a["usage"]["prompt_tokens"] for a, _ in both_successful)
            if both_successful
            else None,
        }
    return summary


def write_report(
    root: Path, config: Config, selection: dict[str, Any], records: list[dict[str, Any]]
) -> None:
    planned_runs = len(selection["selected"]) * config.repetitions * len(config.variants)
    save_json(
        root / "report.json",
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "config": asdict(config),
            "planned_runs": planned_runs,
            "completed_runs": len(records),
            "not_run_runs": max(0, planned_runs - len(records)),
            "status": "complete" if len(records) == planned_runs else "incomplete",
            "task_ids": [item["instance_id"] for item in selection["selected"]],
            "selection_policy": selection.get(
                "selection_policy",
                selection.get(
                    "kind",
                    "Flask cases followed by SymPy >=1.5 in dataset order; base failing/gold passing and baseline regressions passing before model execution",
                ),
            ),
            "dataset": "derived from princeton-nlp/SWE-bench_Lite test"
            if selection.get("kind")
            else "princeton-nlp/SWE-bench_Lite test",
            "original_issue_ids": selection.get("original_issue_ids", []),
            "grading": "native local subset, not official Docker leaderboard",
            "summary": aggregate(records),
            "records": records,
            "execution_phases": [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((root / "execution").glob("*.json"))
            ],
            "interrupted_attempts": [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((root / "interrupted").glob("*/*/*/*/interruption.json"))
            ],
            "boundaries": [
                "Repeated runs are clustered by task; request count is not independent sample size",
                "32K pressure profile must not be pooled with default 200K production",
                "No simulated idle, prescribed read order, injected padding or provided snip IDs",
                "Hidden regression patch is applied after submission in a fresh grading checkout",
                "Failed attempts remain in every all-attempt metric; successful-pair metrics are supplemental",
                "No billing claim without verified endpoint prices; summary usage is included",
                "Native Windows/Python environment differs from official SWE-bench containers",
                "Single-agent evaluation; memory/skills/rules/subagents disabled to isolate compression",
            ],
        },
    )


def freeze_experiment(root: Path, frozen: dict[str, Any]) -> None:
    path = root / "experiment.json"
    normalized = json.loads(json.dumps(frozen))
    if not path.exists():
        save_json(path, normalized)
        return
    previous = json.loads(path.read_text(encoding="utf-8"))
    for candidate in (previous, normalized):
        candidate["config"].pop("parallel", None)
    if previous != normalized:
        raise ValueError("Frozen experiment differs; use a new output directory")
    # Keep the original frozen configuration; actual concurrency is recorded
    # separately for each execution phase, rather than rewriting history.


def preserve_interruption(root: Path, directory: Path) -> None:
    if not (directory / "repo").exists():
        return
    relative = directory.resolve().relative_to((root / "runs").resolve())
    if len(relative.parts) != 3 or (directory / "result.json").exists():
        raise ValueError("Only an incomplete, exact evaluation attempt may be archived")
    destination = root / "interrupted" / relative / uuid.uuid4().hex[:12]
    destination.parent.mkdir(parents=True, exist_ok=True)
    directory.rename(destination)
    requests = []
    for path in sorted((destination / "requests").glob("[0-9][0-9][0-9].json")):
        metrics = path.with_name(path.stem + "-metrics.json")
        requests.append(
            Request(**json.loads(metrics.read_text(encoding="utf-8")))
            if metrics.exists()
            else Request("unknown", 0, "", 0, [], error="Evaluator interrupted this request")
        )
    save_json(
        destination / "interruption.json",
        {
            "instance_id": relative.parts[0],
            "repeat": int(relative.parts[1]),
            "variant": relative.parts[2],
            "reason": "Evaluator paused after endpoint overload; no terminal result was persisted",
            "artifact_directory": str(destination),
            "recorded_at": datetime.now(UTC).isoformat(),
            "usage": repository_usage(requests),
            "request_records": [asdict(r) for r in requests],
        },
    )


async def rescore_capture_errors(root: Path) -> None:
    for path in sorted((root / "runs").glob("*/*/*/result.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "corrupt patch" not in raw.get("validation", {}).get("error", ""):
            continue
        directory = path.parent
        if (directory / "assessment.json").exists():
            continue
        spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
        instance = instance_from_record(spec["instance"])
        patch, files = await source_patch(instance, directory / "repo")
        grading = directory / "regrading" / uuid.uuid4().hex[:12]
        validation = await grade(
            instance,
            Path(spec["instance"]["cache"]),
            Path(spec["instance"]["python"]),
            grading,
            patch,
        )
        correction = {
            "validation": validation,
            "tests_passed": validation["success"],
            "success": bool(patch)
            and validation["success"]
            and raw["exit_reason"] == "completed"
            and not raw["errors"],
            "changed_files": files,
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        }
        save_json(
            directory / "assessment.json",
            {
                "reason": "Evaluator strip() removed a trailing unified-diff context line",
                "correction": correction,
                "regrading_directory": str(grading),
            },
        )
        print(f"RESCORED {directory} passed={validation['success']}", flush=True)


async def run(root: Path, config: Config, manifest_path: Path | None = None) -> None:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    selection_path = root / "selection.json"
    selection = (
        json.loads(selection_path.read_text(encoding="utf-8"))
        if selection_path.exists()
        else await prepare(root, config.cases, manifest_path)
    )
    if len(selection["selected"]) != config.cases:
        raise ValueError("Frozen selection size differs from --cases")
    frozen = {
        "config": asdict(config),
        "task_ids": [item["instance_id"] for item in selection["selected"]],
        "runtime_sha256": implementation_fingerprint(),
    }
    if any(item.get("followup_prompts") for item in selection["selected"]):
        frozen["followup_sha256"] = hashlib.sha256(
            json.dumps(
                [item["followup_prompts"] for item in selection["selected"]],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
    freeze_experiment(root, frozen)
    records = [
        assessed_record(path.parent) for path in sorted((root / "runs").glob("*/*/*/result.json"))
    ]
    semaphore = asyncio.Semaphore(config.parallel)
    stop_dispatch = asyncio.Event()
    phase_path = (
        root
        / "execution"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json")
    )
    phase: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "parallel": config.parallel,
        "completed_before": len(records),
        "harness_sha256": hashlib.sha256(
            await asyncio.to_thread(Path(__file__).read_bytes)
        ).hexdigest(),
    }
    save_json(phase_path, phase)
    write_report(root, config, selection, records)

    async def paired(instance: dict[str, Any], repeat: int) -> None:
        variants = list(config.variants)
        offset = (repeat - 1) % len(variants)
        variants = variants[offset:] + variants[:offset]
        async with semaphore:
            for variant in variants:
                directory = root / "runs" / instance["instance_id"] / str(repeat) / variant
                result_path = directory / "result.json"
                if result_path.exists():
                    continue
                if stop_dispatch.is_set():
                    return
                else:
                    preserve_interruption(root, directory)
                    directory.mkdir(parents=True, exist_ok=True)
                    spec = {
                        "instance": instance,
                        "repeat": repeat,
                        "variant": variant,
                        "max_turns": config.max_turns,
                        "timeout_seconds": config.timeout_seconds,
                    }
                    if instance.get("followup_prompts"):
                        spec["followup_prompts"] = instance["followup_prompts"]
                    save_json(directory / "spec.json", spec)
                    print(f"START {instance['instance_id']}/{variant}/{repeat}", flush=True)
                    environment = dict(os.environ)
                    environment["PYTHONIOENCODING"] = "utf-8"
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
                        timeout_seconds=config.timeout_seconds + 360,
                        env=environment,
                    )
                    (directory / "worker.log").write_text(
                        result.stdout + result.stderr, encoding="utf-8"
                    )
                    if not result_path.exists():
                        failure = f"Worker failed: returncode={result.returncode}, timeout={result.timed_out}; {result.stderr[-1500:]} {result.stdout[-1500:]}"
                        save_json(result_path, failed_worker_record(spec, directory, failure))
                    record = assessed_record(directory)
                    if service_unavailable(record):
                        stop_dispatch.set()
                        print(
                            "CIRCUIT STOP: endpoint unavailable or invalid usage; no new attempts",
                            flush=True,
                        )
                records.append(record)
                write_report(root, config, selection, records)
                print(
                    f"PROGRESS {len(records)}/{len(selection['selected']) * config.repetitions * len(config.variants)} {instance['instance_id']}/{variant}/{repeat} success={record['success']} requests={record['usage']['requests']} input={record['usage']['prompt_tokens']} layers={record['layers']}",
                    flush=True,
                )

    await asyncio.gather(
        *(
            paired(instance, repeat)
            for instance in selection["selected"]
            for repeat in range(1, config.repetitions + 1)
        )
    )
    phase.update(
        ended_at=datetime.now(UTC).isoformat(),
        completed_after=len(records),
        stopped_for_service=stop_dispatch.is_set(),
    )
    save_json(phase_path, phase)
    write_report(root, config, selection, records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--cases", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.worker:
        asyncio.run(attempt(args.worker.resolve()))
        return
    if args.output is None:
        parser.error("--output is required")
    root = args.output.resolve()
    if args.rescore:
        asyncio.run(rescore_capture_errors(root))
    if args.report_only:
        frozen = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
        data = frozen["config"]
        data["variants"] = tuple(data["variants"])
        selection = json.loads((root / "selection.json").read_text(encoding="utf-8"))
        records = [
            assessed_record(path.parent)
            for path in sorted((root / "runs").glob("*/*/*/result.json"))
        ]
        write_report(root, Config(**data), selection, records)
        return
    if args.pilot:
        selection = json.loads((root / "selection.json").read_text(encoding="utf-8"))
        if not selection["selected"]:
            parser.error("No environment-valid case available for pilot")
        pilot_root = root / "pilot"
        save_json(
            pilot_root / "selection.json",
            {**selection, "requested": 1, "selected": selection["selected"][:1]},
        )
        asyncio.run(run(pilot_root, Config(repetitions=1, cases=1, parallel=1)))
        return
    if args.prepare_only:
        asyncio.run(prepare(root, args.cases, args.manifest.resolve() if args.manifest else None))
        return
    config = Config(
        args.repetitions,
        args.parallel,
        args.cases,
        tuple(args.variants),
        args.max_turns,
        args.timeout_seconds,
    )
    asyncio.run(run(root, config, args.manifest.resolve() if args.manifest else None))


if __name__ == "__main__":
    main()
