"""Controlled long-task A/B experiment for context compression.

Unlike the small L1 spill smoke test, this runner keeps one Agent alive for
multiple top-level phases.  Every phase makes a real model request, invokes a
real Carbide tool, appends a large tool result to the conversation, and then
continues the same task.  Compression-on and compression-off runs receive the
same phase prompts and deterministic tool payloads.

Run with a configured Anthropic-compatible endpoint::

    uv run --env-file .env python -m evals.context_efficiency.long_task_ab \
        --repetitions 2 --phases 8 --payload-chars 56000 \
        --compression-budget 100000 \
        --output eval_results/context-efficiency/long-task-ab.json

The runner records exact ``count_tokens`` values for every provider request,
stream usage/cache fields, tool calls, compression events, phase completion,
and the final task result.  This is an end-to-end long-session experiment,
not a benchmark of every possible task or every compression layer.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import statistics
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from carbide.context.snip import SnipCompressor
from carbide.governance.permission import RiskLevel
from carbide.providers.base import Provider
from carbide.sdk import Agent
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.registry import ToolRegistry
from carbide.tools.snip import SnipTool

EXPECTED_FINAL = "LONG_TASK_OK"


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Inputs that define one reproducible long-task workload."""

    repetitions: int = 2
    phases: int = 8
    payload_chars: int = 56_000
    compression_budget: int = 100_000

    def __post_init__(self) -> None:
        if self.repetitions < 1:
            raise ValueError("repetitions must be positive")
        if self.phases < 3:
            raise ValueError("phases must be at least 3 for a long-task run")
        if self.payload_chars <= 50 * 1024:
            raise ValueError("payload_chars must exceed the L1 spill threshold")
        if self.compression_budget < 1:
            raise ValueError("compression_budget must be positive")


class ObservePhaseParams(BaseModel):
    """Arguments exposed to the model for the deterministic observation tool."""

    phase: int = Field(ge=1, description="The current long-task phase number.")


class ObservePhaseTool(Tool):
    """Return a large, deterministic observation for one task phase."""

    name: ClassVar[str] = "observe_phase"
    description: ClassVar[str] = (
        "Read the current phase observation. You must call this exactly once for each "
        "phase before reporting that phase complete."
    )
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = ObservePhaseParams

    def __init__(self, payload_chars: int) -> None:
        self.payload_chars = payload_chars

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, ObservePhaseParams)
        prefix = (
            f"phase={args.phase}\n"
            f"checkpoint=LONG_TASK_PHASE_{args.phase}_CHECKPOINT\n"
            "The observation is deterministic evaluation data.\n"
        )
        lines: list[str] = []
        index = 0
        while len(prefix) + sum(len(line) for line in lines) < self.payload_chars:
            lines.append(
                f"phase-{args.phase}-record-{index:06d}: "
                f"validated-state-{(index * 17 + args.phase * 31) % 100003:05d}; "
                "retain this observation as part of the cumulative task state.\n"
            )
            index += 1
        content = (prefix + "".join(lines))[: self.payload_chars]
        content += f"\nfinal-checkpoint=LONG_TASK_PHASE_{args.phase}_FINAL\n"
        return ToolResult(content=content)


@dataclass(slots=True)
class CapturedRequest:
    """Immutable copy of one provider stream request."""

    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None
    tools: list[dict[str, Any]] | None
    token_count: int | None = None


class RecordingProvider(Provider):
    """Delegate to a live provider while retaining exact request envelopes."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.requests: list[CapturedRequest] = []

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "unknown"))

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        request = CapturedRequest(
            messages=copy.deepcopy(messages),
            system=copy.deepcopy(system),
            tools=copy.deepcopy(tools),
        )
        self.requests.append(request)
        async for chunk in self.inner.stream(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        ):
            yield chunk

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return await self.inner.count_tokens(messages=messages, system=system, tools=tools)

    def estimate_tokens(self, text: str) -> int:
        return self.inner.estimate_tokens(text)

    async def count_captured(self, requests: list[CapturedRequest]) -> list[int]:
        """Count captured request envelopes with the same live Provider."""
        counts: list[int] = []
        client = getattr(self.inner, "client", None)
        if client is None:
            raise RuntimeError("Exact live counting requires a Provider count_tokens client")
        for request in requests:
            envelope: dict[str, Any] = {"model": self.model, "messages": request.messages}
            if request.system:
                envelope["system"] = request.system
            if request.tools:
                envelope["tools"] = request.tools
            counted = await client.messages.count_tokens(**envelope)
            count = int(counted.input_tokens)
            request.token_count = count
            counts.append(count)
        return counts


def _system_prompt() -> str:
    return """You are taking part in a controlled long-task evaluation.

For every phase, you MUST call observe_phase exactly once with the phase number
from the user instruction. Do not claim a phase is complete before the tool
returns. After the tool returns, reply with exactly PHASE_<n>_DONE and nothing
else. At the final verification phase, follow the user's exact final-response
instruction. Do not call any other tool.
"""


def _phase_prompt(phase: int, phases: int) -> str:
    if phase < phases:
        return (
            f"Long task phase {phase} of {phases}. Call observe_phase with phase={phase} "
            f"exactly once, then reply exactly PHASE_{phase}_DONE."
        )
    return (
        f"Long task final verification phase {phase} of {phases}. Call observe_phase "
        f"with phase={phase} exactly once. After it returns, reply exactly {EXPECTED_FINAL} "
        "and nothing else."
    )


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
    """Run one long task with compression either enabled or disabled."""
    from carbide.providers.anthropic import create_anthropic_provider

    inner_provider = create_anthropic_provider()
    provider = RecordingProvider(inner_provider)
    registry = ToolRegistry()
    registry.register(ObservePhaseTool(config.payload_chars))
    agent = Agent(
        provider=provider,
        project_dir=workspace,
        system_prompt=_system_prompt(),
        tool_registry=registry,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
        enable_governance=False,
        enable_session_store=False,
        enable_subagents=False,
        enable_todo_tools=False,
        enable_compression=enabled,
        close_provider=False,
    )
    if not enabled:
        # SDK compression registers Snip automatically. Match the schema prefix
        # in the baseline, with an inert queue that never changes its history.
        registry.register(SnipTool(SnipCompressor()))
    if enabled:
        assert agent.compression_manager is not None
        agent.compression_manager.budget.max_tokens = config.compression_budget

    phase_records: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        for phase in range(1, config.phases + 1):
            request_start = len(provider.requests)
            phase_started = time.perf_counter()
            events = [event async for event in agent.query(_phase_prompt(phase, config.phases))]
            captured = provider.requests[request_start:]
            request_counts = await provider.count_captured(captured)
            compression_events = [
                event.data
                for event in events
                if event.type == "compression" and isinstance(event.data, dict)
            ]
            tool_calls = [
                event.data
                for event in events
                if event.type == "tool_use" and isinstance(event.data, dict)
            ]
            tool_completions = [
                event.data
                for event in events
                if event.type == "tool_complete" and isinstance(event.data, dict)
            ]
            response = _text_from_events(events)
            expected = EXPECTED_FINAL if phase == config.phases else f"PHASE_{phase}_DONE"
            done = next(
                (
                    event.data
                    for event in reversed(events)
                    if event.type == "turn_complete" and isinstance(event.data, dict)
                ),
                {},
            )
            phase_records.append(
                {
                    "phase": phase,
                    "success": response == expected
                    and len(tool_calls) == 1
                    and all(not item.get("is_error", False) for item in tool_completions),
                    "response": response,
                    "expected_response": expected,
                    "tool_calls": len(tool_calls),
                    "tool_errors": sum(
                        1 for item in tool_completions if item.get("is_error", False)
                    ),
                    "request_count": len(captured),
                    "provider_request_tokens": request_counts,
                    "provider_request_tokens_total": sum(request_counts),
                    "usage": _usage_from_events(events),
                    "compression_events": compression_events,
                    "done": done,
                    "duration_ms": round((time.perf_counter() - phase_started) * 1000, 3),
                }
            )
            print(
                f"mode={'on' if enabled else 'off'} phase={phase}/{config.phases} "
                f"requests={len(captured)} tokens={sum(request_counts)} "
                f"success={phase_records[-1]['success']}"
            )
    finally:
        await agent.__aexit__(None, None, None)

    return {
        "enabled": enabled,
        "success": all(item["success"] for item in phase_records),
        "phases_completed": sum(1 for item in phase_records if item["success"]),
        "phases": phase_records,
        "provider_request_tokens_total": sum(
            item["provider_request_tokens_total"] for item in phase_records
        ),
        "provider_request_tokens_peak": max(
            (max(item["provider_request_tokens"], default=0) for item in phase_records),
            default=0,
        ),
        "stream_usage": {
            key: sum(item["usage"][key] for item in phase_records)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "prompt_tokens",
            )
        },
        "compression_layers": sorted(
            {
                layer
                for item in phase_records
                for event in item["compression_events"]
                for layer in event.get("layers", [])
            }
        ),
        "compression_events": sum(len(item["compression_events"]) for item in phase_records),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0.0, "mean": 0.0, "median": 0.0, "p95": 0.0}
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return {
        "count": float(len(values)),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(ordered[index], 3),
    }


def _aggregate(records: list[dict[str, Any]], *, enabled: bool) -> dict[str, Any]:
    selected = [record for record in records if record["enabled"] is enabled]
    totals = [record["provider_request_tokens_total"] for record in selected]
    peaks = [record["provider_request_tokens_peak"] for record in selected]
    durations = [record["duration_ms"] for record in selected]
    return {
        "enabled": enabled,
        "runs": len(selected),
        "successes": sum(1 for record in selected if record["success"]),
        "success_rate": (
            sum(1 for record in selected if record["success"]) / len(selected) if selected else 0.0
        ),
        "provider_request_tokens_total": _summary(totals),
        "provider_request_tokens_peak": _summary(peaks),
        "duration_ms": _summary(durations),
        "stream_usage": {
            key: _summary([record["stream_usage"][key] for record in selected])
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "prompt_tokens",
            )
        },
        "compression_layers": sorted(
            {layer for record in selected for layer in record["compression_layers"]}
        ),
        "compression_events": sum(record["compression_events"] for record in selected),
        "phase_success_rate": (
            sum(phase["success"] for record in selected for phase in record["phases"])
            / (len(selected) * len(selected[0]["phases"]))
            if selected and selected[0]["phases"]
            else 0.0
        ),
    }


async def run_experiment(config: ExperimentConfig) -> dict[str, Any]:
    """Run paired multi-phase tasks and return a JSON report."""
    records: list[dict[str, Any]] = []
    for repetition in range(config.repetitions):
        with tempfile.TemporaryDirectory(prefix="carbide-long-task-ab-") as root:
            workspace = Path(root)
            for enabled in (True, False):
                record = await _run_one(
                    enabled=enabled,
                    config=config,
                    workspace=workspace,
                )
                record["repetition"] = repetition + 1
                records.append(record)

    on = _aggregate(records, enabled=True)
    off = _aggregate(records, enabled=False)
    on_tokens = on["provider_request_tokens_total"]["mean"]
    off_tokens = off["provider_request_tokens_total"]["mean"]
    reduction = (off_tokens - on_tokens) / off_tokens * 100 if off_tokens else 0.0
    return {
        "schema_version": 1,
        "kind": "long-task-live-compression-ab",
        "timestamp": datetime.now(UTC).isoformat(),
        "experiment": {
            "repetitions": config.repetitions,
            "phases": config.phases,
            "payload_chars_per_phase": config.payload_chars,
            "compression_budget": config.compression_budget,
            "workload": "multi-phase real-model task with one deterministic tool call per phase",
            "comparison": (
                "sum of exact Provider count_tokens values for every model request; "
                "stream usage/cache fields are auxiliary"
            ),
            "token_counter": "strict live count_tokens endpoint; failures do not fall back to estimates",
        },
        "summary": {
            "compression_on": on,
            "compression_off": off,
            "provider_request_token_reduction_percent": round(reduction, 3),
            "completion_rate_unchanged": on["success_rate"] == off["success_rate"],
        },
        "records": records,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--phases", type=int, default=8)
    parser.add_argument("--payload-chars", type=int, default=56_000)
    parser.add_argument("--compression-budget", type=int, default=100_000)
    parser.add_argument("--output", type=Path, required=True)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    config = ExperimentConfig(
        repetitions=args.repetitions,
        phases=args.phases,
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
        "provider_request_token_reduction="
        f"{summary['provider_request_token_reduction_percent']:.3f}%"
    )
    print(f"result={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
