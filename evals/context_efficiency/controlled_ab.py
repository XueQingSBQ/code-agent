"""Controlled live A/B experiment for context compression.

This experiment is intentionally narrower than a task-suite benchmark.  It
seeds the same valid conversation history into two Agents, then sends the
same final request with compression enabled and disabled.  The history
contains large tool results so L1 spill is exercised deterministically.  The
Provider's exact ``count_tokens`` result is the primary comparison; local
estimates, streaming usage, and compression events are retained as diagnostics.

Run with the endpoint configured in ``.env``::

    uv run --env-file .env python -m evals.context_efficiency.controlled_ab \
        --repetitions 5 --output eval_results/context-efficiency/controlled-ab.json

This does consume Provider quota.  It does not claim that L1-L4.5 are all
represented: the controlled payload is designed to isolate L1 spill.  A
separate task-suite experiment is required to measure end-to-end task impact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.sdk import Agent

FINAL_PROMPT = "Reply with exactly CONTROLLED_COMPRESSION_OK and nothing else."
EXPECTED_RESPONSE = "CONTROLLED_COMPRESSION_OK"


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    repetitions: int = 5
    segments: int = 3
    payload_chars: int = 60_000
    compression_budget: int = 50_000

    def __post_init__(self) -> None:
        if self.repetitions < 1:
            raise ValueError("repetitions must be positive")
        if self.segments < 1:
            raise ValueError("segments must be positive")
        if self.payload_chars < 50 * 1024:
            raise ValueError("payload_chars must exceed the L1 spill threshold")
        if self.compression_budget < 1:
            raise ValueError("compression_budget must be positive")


def _seed_history(*, segments: int, payload_chars: int) -> list[dict[str, Any]]:
    """Build a valid alternating conversation with large tool results."""
    messages: list[dict[str, Any]] = []
    for index in range(segments):
        tool_id = f"controlled-tool-{index}"
        messages.extend(
            [
                {
                    "role": "user",
                    "content": f"Inspect controlled segment {index}.",
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "read",
                            "input": {"path": f"segment-{index}.txt"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": (f"segment-{index}\n" + "x" * payload_chars),
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Segment recorded."}],
                },
            ]
        )
    return messages


def _text_from_events(events: list[Any]) -> str:
    parts: list[str] = []
    for event in events:
        if event.type != "chunk" or event.data is None:
            continue
        content = getattr(event.data, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts).strip()


def _usage_from_events(events: list[Any]) -> dict[str, int]:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    for event in events:
        if event.type != "turn_complete" or not isinstance(event.data, dict):
            continue
        for key in usage:
            usage[key] += int(event.data.get(key, 0) or 0)
    usage["prompt_tokens"] = (
        usage["input_tokens"] + usage["cache_read_tokens"] + usage["cache_creation_tokens"]
    )
    return usage


async def _run_one(
    *,
    enabled: bool,
    config: ExperimentConfig,
    workspace: Path,
) -> dict[str, Any]:
    """Run one side of the controlled comparison."""
    agent = Agent(
        project_dir=workspace,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_governance=False,
        enable_session_store=False,
        enable_compression=enabled,
    )
    seeded_history = _seed_history(
        segments=config.segments,
        payload_chars=config.payload_chars,
    )
    agent.loop.state.messages.extend(seeded_history)
    if enabled:
        assert agent.compression_manager is not None
        agent.compression_manager.budget.max_tokens = config.compression_budget

    budget = agent.compression_manager.budget if agent.compression_manager is not None else None
    user_message_tokens = (
        budget.estimate_tokens(FINAL_PROMPT).tokens
        if budget is not None
        else len(FINAL_PROMPT) // 4
    )
    estimated_before = (
        sum(
            budget.estimate_tokens(message).tokens if budget is not None else len(str(message)) // 4
            for message in seeded_history
        )
        + user_message_tokens
    )

    started = time.perf_counter()
    try:
        system_before = await agent.loop._build_system_prompt(
            base_prompt=agent.system_prompt,
            user_message=FINAL_PROMPT,
        )
        count_before = await agent.provider.count_tokens(
            messages=[*seeded_history, {"role": "user", "content": FINAL_PROMPT}],
            system=system_before,
            tools=[],
        )
        events = [event async for event in agent.query(FINAL_PROMPT, tools=[])]
        elapsed_ms = (time.perf_counter() - started) * 1000
        compression_events = [
            event.data
            for event in events
            if event.type == "compression" and isinstance(event.data, dict)
        ]
        done_events = [
            event.data for event in events if event.type == "done" and isinstance(event.data, dict)
        ]
        response = _text_from_events(events)
        usage = _usage_from_events(events)
        request_messages = agent.loop.state.messages
        if request_messages and request_messages[-1].get("role") == "assistant":
            request_messages = request_messages[:-1]
        estimated_after = sum(
            budget.estimate_tokens(message).tokens if budget is not None else len(str(message)) // 4
            for message in request_messages
        )
        count_after = await agent.provider.count_tokens(
            messages=request_messages,
            system=agent.loop.rendered_request_system or agent.system_prompt,
            tools=[],
        )
        return {
            "enabled": enabled,
            "success": response == EXPECTED_RESPONSE,
            "response": response,
            "usage": usage,
            "estimated_request_tokens_before": estimated_before,
            "estimated_request_tokens_after": estimated_after,
            "provider_count_tokens_before": count_before,
            "provider_count_tokens_after": count_after,
            "compression_events": compression_events,
            "done": done_events[-1] if done_events else {},
            "duration_ms": round(elapsed_ms, 3),
        }
    finally:
        await agent.__aexit__(None, None, None)


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "mean": 0.0, "median": 0.0, "p95": 0.0}
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "count": float(len(values)),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(ordered[p95_index], 3),
    }


def _aggregate(records: list[dict[str, Any]], *, enabled: bool) -> dict[str, Any]:
    selected = [record for record in records if record["enabled"] is enabled]
    input_tokens = [record["usage"]["input_tokens"] for record in selected]
    prompt_tokens = [record["usage"]["prompt_tokens"] for record in selected]
    estimated_before = [record["estimated_request_tokens_before"] for record in selected]
    estimated_after = [record["estimated_request_tokens_after"] for record in selected]
    provider_count_before = [record["provider_count_tokens_before"] for record in selected]
    provider_count_after = [record["provider_count_tokens_after"] for record in selected]
    output_tokens = [record["usage"]["output_tokens"] for record in selected]
    durations = [record["duration_ms"] for record in selected]
    return {
        "enabled": enabled,
        "runs": len(selected),
        "successes": sum(1 for record in selected if record["success"]),
        "success_rate": (
            sum(1 for record in selected if record["success"]) / len(selected) if selected else 0.0
        ),
        "input_tokens": _summary(input_tokens),
        "prompt_tokens": _summary(prompt_tokens),
        "estimated_request_tokens_before": _summary(estimated_before),
        "estimated_request_tokens_after": _summary(estimated_after),
        "provider_count_tokens_before": _summary(provider_count_before),
        "provider_count_tokens_after": _summary(provider_count_after),
        "output_tokens": _summary(output_tokens),
        "duration_ms": _summary(durations),
        "layers": sorted(
            {
                layer
                for record in selected
                for event in record["compression_events"]
                for layer in event.get("layers", [])
            }
        ),
    }


async def run_experiment(config: ExperimentConfig) -> dict[str, Any]:
    """Run paired compression-on/off requests and return a JSON report."""
    records: list[dict[str, Any]] = []
    for repetition in range(config.repetitions):
        with tempfile.TemporaryDirectory(prefix="carbide-compression-ab-") as root:
            workspace = Path(root)
            for enabled in (True, False):
                record = await _run_one(
                    enabled=enabled,
                    config=config,
                    workspace=workspace,
                )
                record["repetition"] = repetition + 1
                records.append(record)
                mode = "on" if enabled else "off"
                print(
                    f"rep={repetition + 1}/{config.repetitions} mode={mode} "
                    f"input={record['usage']['input_tokens']} "
                    f"success={record['success']}"
                )

    on = _aggregate(records, enabled=True)
    off = _aggregate(records, enabled=False)
    on_mean = on["prompt_tokens"]["mean"]
    off_mean = off["prompt_tokens"]["mean"]
    reduction = (off_mean - on_mean) / off_mean * 100 if off_mean else 0.0
    on_count_mean = on["provider_count_tokens_after"]["mean"]
    off_count_mean = off["provider_count_tokens_after"]["mean"]
    count_reduction = (
        (off_count_mean - on_count_mean) / off_count_mean * 100 if off_count_mean else 0.0
    )
    return {
        "schema_version": 1,
        "kind": "controlled-live-compression-ab",
        "timestamp": datetime.now(UTC).isoformat(),
        "experiment": {
            "repetitions": config.repetitions,
            "segments": config.segments,
            "payload_chars_per_segment": config.payload_chars,
            "compression_budget": config.compression_budget,
            "final_prompt": FINAL_PROMPT,
            "comparison": (
                "Provider count_tokens before/after compression; stream usage is auxiliary "
                "because cache fields are reported separately"
            ),
        },
        "summary": {
            "compression_on": on,
            "compression_off": off,
            "stream_usage_prompt_token_reduction_percent": round(reduction, 3),
            "provider_count_token_reduction_percent": round(count_reduction, 3),
            "uncached_input_token_reduction_percent": round(
                (
                    (off["input_tokens"]["mean"] - on["input_tokens"]["mean"])
                    / off["input_tokens"]["mean"]
                    * 100
                )
                if off["input_tokens"]["mean"]
                else 0.0,
                3,
            ),
            "completion_rate_unchanged": on["success_rate"] == off["success_rate"],
        },
        "records": records,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--segments", type=int, default=3)
    parser.add_argument("--payload-chars", type=int, default=60_000)
    parser.add_argument("--compression-budget", type=int, default=50_000)
    parser.add_argument("--output", type=Path, required=True)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    config = ExperimentConfig(
        repetitions=args.repetitions,
        segments=args.segments,
        payload_chars=args.payload_chars,
        compression_budget=args.compression_budget,
    )
    report = await run_experiment(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = report["summary"]
    print(
        f"provider_count_token_reduction={summary['provider_count_token_reduction_percent']:.3f}%"
    )
    print(f"result={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
