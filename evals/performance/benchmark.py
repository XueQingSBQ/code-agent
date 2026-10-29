"""Run repeatable offline performance baselines.

This benchmark deliberately separates local runtime costs from provider
latency. It measures filesystem tools, a large-project search, context
compression, and concurrent Agent orchestration with a deterministic provider.
It does not claim to measure model/API latency; live endpoint measurements are
covered by ``tests/test_live_api_matrix.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.context.budget import TokenBudget
from carbide.context.manager import CompressionManager
from carbide.engine.loop import QueryEvent
from carbide.providers.base import Provider, StreamEnd, StreamTextDelta, Usage
from carbide.sdk import Agent
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.fs import ReadTool
from carbide.tools.search import GlobTool, GrepTool

Sample = Callable[[], Awaitable[object]]


class BenchmarkProvider(Provider):
    """Deterministic provider for measuring Agent orchestration only."""

    @property
    def model(self) -> str:
        return "benchmark-provider"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> Any:
        del system, tools, max_tokens, temperature, kwargs
        yield StreamTextDelta("benchmark-ok")
        yield StreamEnd(
            usage=Usage(
                input_tokens=max(1, len(messages)),
                output_tokens=1,
            )
        )

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        del system, tools
        return max(1, len(messages))

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(samples: list[float], successes: int) -> dict[str, Any]:
    return {
        "count": len(samples),
        "successes": successes,
        "success_rate": successes / len(samples) if samples else 0.0,
        "min_ms": min(samples) if samples else 0.0,
        "avg_ms": sum(samples) / len(samples) if samples else 0.0,
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
        "max_ms": max(samples) if samples else 0.0,
    }


async def _measure(operation: Sample, iterations: int) -> dict[str, Any]:
    samples: list[float] = []
    successes = 0
    for _ in range(iterations):
        started = time.perf_counter()
        try:
            await operation()
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            samples.append(elapsed)
            continue
        samples.append((time.perf_counter() - started) * 1000)
        successes += 1
    return _summary(samples, successes)


async def _invoke(tool: Tool, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    result = await tool.run(type(tool).params.model_validate(arguments), context)
    if result.is_error:
        raise RuntimeError(result.content)
    return result


def _create_corpus(root: Path, file_count: int) -> None:
    for index in range(file_count):
        extension = ".py" if index % 2 == 0 else ".txt"
        path = root / "src" / f"module_{index:05d}{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = "benchmark_needle" if index % 10 == 0 else "ordinary_content"
        path.write_text(f"# {marker}\nvalue = {index}\n", encoding="utf-8")
    (root / "README.md").write_text("benchmark workspace\n", encoding="utf-8")


async def _benchmark_tools(root: Path, iterations: int) -> dict[str, Any]:
    context = ToolContext(workspace=root, shell="gitbash")
    read = ReadTool()
    glob = GlobTool()
    grep = GrepTool()
    return {
        "read_single_file": await _measure(
            lambda: _invoke(read, {"path": "src/module_00000.py"}, context),
            iterations,
        ),
        "glob_python_files": await _measure(
            lambda: _invoke(glob, {"pattern": "src/**/*.py"}, context),
            iterations,
        ),
        "grep_files_with_matches": await _measure(
            lambda: _invoke(
                grep,
                {"pattern": "benchmark_needle", "path": "src"},
                context,
            ),
            iterations,
        ),
    }


def _compression_messages() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"benchmark-{index}",
                    "content": "x" * 20_000,
                }
            ],
        }
        for index in range(24)
    ]


async def _benchmark_compression(root: Path, iterations: int) -> dict[str, Any]:
    manager = CompressionManager(
        workspace=root,
        budget=TokenBudget(max_tokens=10_000, compression_threshold=0.8),
        spill_dir=root / ".carbide" / "spill",
    )

    async def operation() -> object:
        messages = _compression_messages()
        return await manager.compress(messages)

    return await _measure(operation, iterations)


async def _benchmark_concurrent_agents(
    root: Path,
    concurrency: int,
) -> dict[str, Any]:
    async def run_one() -> float:
        started = time.perf_counter()
        agent = Agent(
            provider=BenchmarkProvider(),
            project_dir=root,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
            enable_governance=False,
            enable_session_store=False,
        )
        try:
            events = [event async for event in agent.query("benchmark")]
            if not any(isinstance(event, QueryEvent) and event.type == "turn_complete" for event in events):
                raise RuntimeError("benchmark Agent did not complete")
        finally:
            await agent.__aexit__(None, None, None)
        return (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    durations = await asyncio.gather(*(run_one() for _ in range(concurrency)))
    total_ms = (time.perf_counter() - started) * 1000
    return {
        "concurrency": concurrency,
        "total_wall_ms": total_ms,
        "throughput_agents_per_second": concurrency / (total_ms / 1000)
        if total_ms > 0
        else 0.0,
        "per_agent": _summary(list(durations), len(durations)),
    }


async def run_benchmark(
    *,
    iterations: int,
    file_count: int,
    concurrency: int,
) -> dict[str, Any]:
    if iterations < 1 or file_count < 1 or concurrency < 1:
        raise ValueError("iterations, file_count and concurrency must be positive")

    temporary_root = Path(tempfile.mkdtemp(prefix="carbide-perf-"))
    try:
        _create_corpus(temporary_root, file_count)
        return {
            "schema_version": 1,
            "kind": "offline-performance-baseline",
            "timestamp": datetime.now(UTC).isoformat(),
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "cpu": platform.processor(),
            },
            "parameters": {
                "iterations": iterations,
                "file_count": file_count,
                "concurrency": concurrency,
            },
            "results": {
                "tools": await _benchmark_tools(temporary_root, iterations),
                "compression": await _benchmark_compression(temporary_root, iterations),
                "concurrent_agents": await _benchmark_concurrent_agents(
                    temporary_root,
                    concurrency,
                ),
            },
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--files", type=int, default=1_000, dest="file_count")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    report = await run_benchmark(
        iterations=args.iterations,
        file_count=args.file_count,
        concurrency=args.concurrency,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is None:
        print(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Performance baseline written to {args.output}")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
