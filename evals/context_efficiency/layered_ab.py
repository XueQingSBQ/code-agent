"""Live, paired compression evaluations with state recall and executable code.

Each run grows one real Agent conversation through twelve file-reading stages.
The final stage writes code which an independent subprocess checks. Scenarios
isolate spill, explicit snip, idle micro-compaction, and summary/restoration.
L3's idle interval is simulated; the runner never claims an hour actually passed.
Provider-reported usage includes the extra L4 summary calls, including failures.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import statistics
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.context.compact import CompactCompressor
from carbide.context.snip import SnipCompressor
from carbide.engine.repair import validate_message_invariants
from carbide.providers.base import Provider, StreamEnd, StreamTextDelta
from carbide.sdk import Agent
from carbide.tools.fs import ReadTool, WriteTool
from carbide.tools.registry import ToolRegistry
from carbide.tools.snip import SnipTool

SCENARIOS = ("spill", "snip", "idle", "compact")
SYSTEM = """You are executing a controlled coding task in a temporary workspace.
Every numbered phase requires reading active.py exactly once. Its CONFIG_UPDATE
comment changes the cumulative requirements; other log lines are irrelevant.
Return ONLY a JSON checkpoint: {"phase": N, "state": {all requirements so far}}.
Keep decisions from earlier phases; do not reset state to the latest update.
The state object must contain only keys introduced by CONFIG_UPDATE. Keep raw
source code and file metadata outside that object.
If a phase lists obsolete read IDs, call snip with exactly those IDs after reading.
Never snip checkpoint text. At the final implementation phase write solution.py
and receipt.json, then reply DONE. Read active.py again only if its current source
is absent from your context. Read contract.py only when explicitly instructed,
or when its required CONTRACT_NONCE is missing after compaction. Use no other files.
"""


@dataclass(frozen=True)
class Config:
    repetitions: int = 3
    phases: int = 12
    payload_chars: int = 16_000
    parallel: int = 2
    timeout_seconds: int = 180
    scenarios: tuple[str, ...] = SCENARIOS
    variants: tuple[str, ...] = ("off", "summary_only", "full")

    def __post_init__(self) -> None:
        if self.repetitions < 1 or self.phases < 12 or self.parallel < 1:
            raise ValueError("Use positive repetitions/parallel and at least twelve phases")
        if not 4_000 <= self.payload_chars <= 30_000:
            raise ValueError("Medium payloads must stay below the 50 KiB L1 threshold")
        if not self.scenarios or set(self.scenarios) - set(SCENARIOS):
            raise ValueError("Unknown or empty scenario selection")
        if not self.variants or set(self.variants) - {"off", "summary_only", "full"}:
            raise ValueError("Unknown or empty variant selection")


@dataclass
class Request:
    purpose: str
    message_count: int
    envelope_sha256: str
    estimated_envelope_tokens: int
    pairing_violations: list[str]
    usage: dict[str, int] | None = None
    response: str = ""
    error: str | None = None
    duration_ms: float = 0


class MeteredProvider(Provider):
    """Record every real stream, including Provider.complete's summary stream."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.requests: list[Request] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        envelope = json.dumps(
            {"messages": messages, "system": system, "tools": tools},
            sort_keys=True,
            ensure_ascii=False,
        )
        request = Request(
            purpose="summary" if system == CompactCompressor.SUMMARY_TEMPLATE else "task",
            message_count=len(messages),
            envelope_sha256=hashlib.sha256(envelope.encode()).hexdigest(),
            estimated_envelope_tokens=self.inner.estimate_tokens(envelope),
            pairing_violations=validate_message_invariants(messages),
        )
        self.requests.append(request)
        started = time.perf_counter()
        try:
            if request.pairing_violations:
                raise ValueError("Compression produced invalid tool pairing")
            async for chunk in self.inner.stream(
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            ):
                if isinstance(chunk, StreamTextDelta):
                    request.response += chunk.text
                if isinstance(chunk, StreamEnd):
                    request.usage = asdict(chunk.usage)
                yield chunk
        except Exception as exc:
            request.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            request.duration_ms = round((time.perf_counter() - started) * 1000, 3)

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return await self.inner.count_tokens(messages, system, tools)

    def estimate_tokens(self, text: str) -> int:
        return self.inner.estimate_tokens(text)


def updates(seed: int) -> list[dict[str, Any]]:
    """Requirements are introduced in the middle, then selectively revised."""
    return [
        {},
        {"multiplier": 3 + seed},
        {"cap": 120 + seed},
        {"max_retries": 2},
        {"penalty": 4 + seed},
        {"reject_negative": True},
        {"precision": 2},
        {"tag": f"quote-v{seed}"},
        {"cap": 150 + seed},
        {"max_retries": 3},
        {},
        {},
    ]


def expected_states(seed: int, phases: int) -> list[dict[str, Any]]:
    state: dict[str, Any] = {}
    states = []
    changes = updates(seed)
    for phase in range(phases):
        state.update(changes[phase] if phase < len(changes) else {})
        states.append(copy.deepcopy(state))
    return states


def observation(phase: int, seed: int, payload_chars: int) -> str:
    changes = updates(seed)
    change = changes[phase - 1] if phase <= len(changes) else {}
    head = f"# CONFIG_UPDATE = {json.dumps(change, sort_keys=True)}\n"
    head += f"SOURCE_MARKER = 'source-{seed}-{phase}-verified'\n"
    line = f"# observation-{phase}: verified irrelevant trace; no requirements in this record.\n"
    return (
        head + (line * ((payload_chars - len(head)) // len(line) + 1))[: payload_chars - len(head)]
    )


def parse_checkpoint(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(stripped.splitlines()[1:-1])
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def last_response(agent: Agent) -> str:
    for message in reversed(agent.loop.state.messages):
        if message.get("role") == "assistant":
            content = message.get("content", [])
            if isinstance(content, list):
                return "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
    return ""


def usage_totals(requests: list[Request]) -> dict[str, Any]:
    measured = [request for request in requests if request.usage is not None]
    total = {
        key: sum((request.usage or {}).get(key, 0) for request in measured)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    }
    # Anthropic-compatible usage reports uncached input and cache fields separately.
    total["prompt_tokens"] = (
        total["input_tokens"] + total["cache_read_tokens"] + total["cache_creation_tokens"]
    )
    total["all_tokens"] = total["prompt_tokens"] + total["output_tokens"]
    return {
        **total,
        "requests": len(requests),
        "measured_requests": len(measured),
        "complete": len(measured) == len(requests),
        "cache_hit_ratio": total["cache_read_tokens"] / total["prompt_tokens"]
        if total["prompt_tokens"]
        else 0,
    }


async def validate_code(
    workspace: Path, state: dict[str, Any], marker: str, contract_nonce: str | None = None
) -> dict[str, Any]:
    """Independent, bounded process: validate behavior, not a completion marker."""
    program = """import importlib.util, json, sys
from pathlib import Path
expected = json.loads(sys.argv[1])
marker = sys.argv[2]
checks = {}
receipt = None
try:
    receipt = json.loads(Path('receipt.json').read_text(encoding='utf-8'))
    checks['requirements'] = receipt.get('state') == expected
    checks['source_marker'] = receipt.get('source_marker') == marker
    if sys.argv[3]:
        checks['contract_nonce'] = receipt.get('contract_nonce') == sys.argv[3]
    spec = importlib.util.spec_from_file_location('solution', 'solution.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, amount, retries in [('zero', 0, 0), ('normal', 10, 0),
                                  ('penalty', 10, 2), ('cap', 1000, 0),
                                  ('precision', 1.1234, 1), ('floor', 0, 3)]:
        value = round(max(0, min(amount * expected['multiplier']
                      - retries * expected['penalty'], expected['cap'])), expected['precision'])
        checks[name] = module.quote(amount, retries) == value
    for name, amount, retries in [('negative', -1, 0), ('retry_limit', 1, 4)]:
        try:
            module.quote(amount, retries)
            checks[name] = False
        except ValueError:
            checks[name] = True
except Exception as exc:
    checks['load_error'] = str(exc)
print(json.dumps({'checks': checks, 'receipt': receipt}))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        program,
        json.dumps(state),
        marker,
        contract_nonce or "",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return {"success": False, "error": "validation timeout"}
    payload = parse_checkpoint(stdout.decode(errors="replace")) or {}
    checks = payload.get("checks", {})
    return {
        "success": process.returncode == 0
        and len(checks) == 10 + bool(contract_nonce)
        and all(value is True for value in checks.values()),
        "checks": checks,
        "receipt": payload.get("receipt"),
        "stderr": stderr.decode(errors="replace")[-1000:],
    }


def configure(agent: Agent, scenario: str, variant: str) -> None:
    manager = agent.compression_manager
    if manager is None:
        return
    manager.budget.max_tokens = 22_000 if scenario == "spill" else 200_000
    if scenario == "compact":
        manager.budget.max_tokens = 24_000
    if variant == "summary_only":
        # All compact-scenario results are below L1's threshold; no snip and no
        # idle interval are requested. This isolates L4 from L4.5 without mocks.
        manager.l45_restore.max_files = 0


async def run_one(config: Config, scenario: str, variant: str, seed: int) -> dict[str, Any]:
    from carbide.providers.anthropic import create_anthropic_provider

    started = time.perf_counter()
    provider = MeteredProvider(create_anthropic_provider())
    registry = ToolRegistry()
    registry.register_all([ReadTool(), WriteTool()])
    # Keep identical tool schemas in the baseline, but do not apply its marks.
    if variant == "off":
        registry.register(SnipTool(SnipCompressor()))
    phases: list[dict[str, Any]] = []
    compression_events: list[dict[str, Any]] = []
    obsolete_ids: list[str] = []
    error: str | None = None
    validation: dict[str, Any] = {"success": False}
    artifacts: dict[str, str] = {}
    final_reads = 0
    final_compressions = 0
    with tempfile.TemporaryDirectory(prefix="carbide-layered-ab-") as root:
        workspace = Path(root)
        agent = Agent(
            provider=provider,
            project_dir=workspace,
            tool_registry=registry,
            system_prompt=SYSTEM,
            enable_compression=variant != "off",
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_governance=False,
            enable_session_store=False,
            enable_subagents=False,
            enable_todo_tools=False,
            max_turns=8,
        )
        configure(agent, scenario, variant)
        states = expected_states(seed, config.phases)
        nonce = f"contract-{seed}-c72d" if scenario == "compact" else None
        if nonce:
            (workspace / "contract.py").write_text(
                f"CONTRACT_NONCE = '{nonce}'\n", encoding="utf-8"
            )
        try:
            for phase in range(1, config.phases + 1):
                size = 56_000 if scenario == "spill" else config.payload_chars
                (workspace / "active.py").write_text(
                    observation(phase, seed, size), encoding="utf-8"
                )
                if scenario == "idle" and phase == 9 and agent.compression_manager:
                    # Exercise the production 3600-second condition, without
                    # sleeping an hour or reducing its configured threshold.
                    agent.compression_manager.l3_micro.last_call_time = time.time() - 3_601
                prompt = f"Phase {phase}. Read active.py; return the cumulative JSON checkpoint."
                if scenario == "compact" and phase == 6:
                    prompt += " Also read contract.py once; its raw source is needed at final implementation."
                if scenario == "snip" and phase == 6:
                    prompt += f" Obsolete read IDs to snip: {json.dumps(obsolete_ids[:3])}."
                request_start = len(provider.requests)
                async with asyncio.timeout(config.timeout_seconds):
                    events = [event async for event in agent.query(prompt)]
                calls = [event.data for event in events if event.type == "tool_use"]
                obsolete_ids.extend(call["id"] for call in calls if call["name"] == "read")
                compression_events.extend(
                    event.data for event in events if event.type == "compression"
                )
                checkpoint = parse_checkpoint(last_response(agent))
                data = checkpoint.get("state", {}) if checkpoint else {}
                expected = states[phase - 1]
                correct_fields = sum(data.get(key) == value for key, value in expected.items())
                phases.append(
                    {
                        "phase": phase,
                        "checkpoint": checkpoint,
                        "success": checkpoint == {"phase": phase, "state": expected},
                        "correct_fields": correct_fields,
                        "expected_fields": len(expected),
                        "read_calls": sum(call["name"] == "read" for call in calls),
                        "usage": usage_totals(provider.requests[request_start:]),
                        "errors": [event.data for event in events if event.type == "error"],
                    }
                )
                if phases[-1]["errors"]:
                    raise RuntimeError(str(phases[-1]["errors"]))
                print(
                    f"{scenario}/{variant} seed={seed} phase={phase}/{config.phases} "
                    f"recall={correct_fields}/{len(expected)}",
                    flush=True,
                )
            final_prompt = (
                "Implement the accumulated requirements now. Write solution.py with "
                "quote(amount, retries=0): reject negative amount or retries above max_retries "
                "with ValueError; compute round(max(0, min(amount * multiplier - retries * "
                "penalty, cap)), precision). Use the accumulated numeric settings, not invented "
                "defaults. Write receipt.json containing state (all accumulated settings) and "
                "source_marker (the exact SOURCE_MARKER string from the current active.py). "
                "Do not read active.py if its current source is already in context. Reply DONE."
            )
            if nonce:
                final_prompt += (
                    " receipt.json must also contain contract_nonce, the exact CONTRACT_NONCE "
                    "from contract.py. Read contract.py only if its source is missing from context."
                )
            async with asyncio.timeout(config.timeout_seconds):
                events = [event async for event in agent.query(final_prompt)]
            final_reads = sum(
                event.type == "tool_use" and event.data.get("name") == "read" for event in events
            )
            final_compressions = sum(event.type == "compression" for event in events)
            compression_events.extend(event.data for event in events if event.type == "compression")
            validation = await validate_code(
                workspace, states[-1], f"source-{seed}-{config.phases}-verified", nonce
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            for name in ("solution.py", "receipt.json"):
                path = workspace / name
                if path.is_file():
                    artifacts[name] = path.read_text(encoding="utf-8")[:16_000]
            await agent.__aexit__(None, None, None)
    task = [request for request in provider.requests if request.purpose == "task"]
    summaries = [request for request in provider.requests if request.purpose == "summary"]
    return {
        "scenario": scenario,
        "variant": variant,
        "seed": seed,
        "model": str(getattr(provider.inner, "model", "unknown")),
        "success": error is None
        and validation["success"]
        and len(phases) == config.phases
        and all(phase["success"] for phase in phases),
        "error": error,
        "validation": validation,
        "artifacts": artifacts,
        "phases": phases,
        "usage": usage_totals(provider.requests),
        "task_usage": usage_totals(task),
        "summary_usage": usage_totals(summaries),
        "summary_calls": len(summaries),
        "peak_prompt_tokens": max(
            (usage_totals([request])["prompt_tokens"] for request in provider.requests), default=0
        ),
        "final_reads": final_reads,
        "final_compressions": final_compressions,
        "compression_events": compression_events,
        "layers": sorted({layer for event in compression_events for layer in event["layers"]}),
        "requests": [asdict(request) for request in provider.requests],
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def stats(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0,
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons = []
    groups = sorted({(record["scenario"], record["variant"]) for record in records})
    for scenario, variant in groups:
        selected = [
            record
            for record in records
            if (record["scenario"], record["variant"]) == (scenario, variant)
        ]
        baselines = {
            record["seed"]: record
            for record in records
            if record["scenario"] == scenario and record["variant"] == "off"
        }
        savings = []
        for record in selected:
            baseline = baselines.get(record["seed"])
            # Never drop failed tasks to improve the reported saving. Missing
            # usage makes a pair unmeasurable, rather than a zero-token success.
            if baseline and record["usage"]["complete"] and baseline["usage"]["complete"]:
                before = baseline["usage"]["prompt_tokens"]
                if before:
                    savings.append((before - record["usage"]["prompt_tokens"]) / before * 100)
        expected_fields = sum(
            phase["expected_fields"] for record in selected for phase in record["phases"]
        )
        comparisons.append(
            {
                "scenario": scenario,
                "variant": variant,
                "runs": len(selected),
                "successes": sum(record["success"] for record in selected),
                "code_passes": sum(record["validation"]["success"] for record in selected),
                "field_recall": sum(
                    phase["correct_fields"] for record in selected for phase in record["phases"]
                )
                / expected_fields
                if expected_fields
                else 0,
                "prompt_tokens": stats([record["usage"]["prompt_tokens"] for record in selected]),
                "all_tokens": stats([record["usage"]["all_tokens"] for record in selected]),
                "peak_prompt_tokens": stats([record["peak_prompt_tokens"] for record in selected]),
                "paired_prompt_reduction_percent": stats(savings) if savings else None,
                "summary_calls": sum(record["summary_calls"] for record in selected),
                "summary_prompt_tokens": sum(
                    record["summary_usage"]["prompt_tokens"] for record in selected
                ),
                "final_reads": sum(record["final_reads"] for record in selected),
                "layers": sorted({layer for record in selected for layer in record["layers"]}),
            }
        )
    return comparisons


def write_report(path: Path, config: Config, records: list[dict[str, Any]], complete: bool) -> None:
    report = {
        "schema_version": 1,
        "kind": "layered-live-coding-ab",
        "timestamp": datetime.now(UTC).isoformat(),
        "complete": complete,
        "config": asdict(config),
        "measurement": "provider-reported uncached + cache-read + cache-write input, including L4 calls",
        "limitations": [
            "synthetic coding workload; not SWE-bench",
            "L3 idle interval simulated; no wall-clock cache-expiry claim",
            "compact scenario uses an accelerated 24000-token budget",
            "explicit snip IDs supplied; autonomous pruning selection not evaluated",
            "API usage is gateway-reported, not an independent tokenizer measurement",
        ],
        "summary": aggregate(records),
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # The evaluator writes its own generated data; no source files are edited.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


async def run(config: Config, output: Path) -> None:
    records: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(config.parallel)

    async def paired(scenario: str, seed: int) -> None:
        variants = ["off", "full"] if scenario != "compact" else ["off", "summary_only", "full"]
        variants = [variant for variant in variants if variant in config.variants]
        if not variants:
            return
        # Rotate order within each scenario so the baseline is not always cold.
        offset = (seed - 1) % len(variants)
        variants = variants[offset:] + variants[:offset]
        async with semaphore:
            for variant in variants:
                record = await run_one(config, scenario, variant, seed)
                records.append(record)
                write_report(output, config, records, complete=False)
                print(
                    f"FINISHED {scenario}/{variant} seed={seed} success={record['success']} "
                    f"prompt={record['usage']['prompt_tokens']} layers={record['layers']}",
                    flush=True,
                )

    await asyncio.gather(
        *(
            paired(scenario, seed)
            for seed in range(1, config.repetitions + 1)
            for scenario in config.scenarios
        )
    )
    write_report(output, config, records, complete=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--phases", type=int, default=12)
    parser.add_argument("--payload-chars", type=int, default=16_000)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=("off", "summary_only", "full"),
        default=["off", "summary_only", "full"],
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = Config(
        args.repetitions,
        args.phases,
        args.payload_chars,
        args.parallel,
        scenarios=tuple(args.scenarios),
        variants=tuple(args.variants),
    )
    asyncio.run(run(config, args.output))


if __name__ == "__main__":
    main()
