"""Live concurrency-1 vs concurrency-2 generic Worker pilot on real issue transplants.

This compares Worker scheduling, NOT a natural single-Agent against an entire
multi-Agent product. The parent harness seeds a shared Todo and never assigns an
item to a Worker. Both arms use identical tasks, tools and model. Independent
regression tests grade only production source patches, never worker-written tests.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from carbide.agents.domain import SubagentRequest, SubagentResult
from carbide.engine.repair import verify_pairing
from carbide.providers.anthropic import AnthropicProvider
from carbide.providers.base import Provider, StreamEnd, StreamToolUse
from carbide.sdk import Agent
from carbide.todo.repository import TodoRepository
from carbide.todo.service import TodoService
from carbide.tools.defaults import create_default_registry
from evals.context_efficiency.repository_ab import implementation_fingerprint, instance_from_record
from evals.context_efficiency.repository_environment import (
    checked,
    checkout,
    grade,
    production_path,
    save_json,
)


class ConcurrentMeter(Provider):
    """Request-scoped metrics: safe when parent and children share one Provider."""

    def __init__(self, inner: Provider, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.requests: list[dict[str, Any]] = []
        self.active = 0
        self.peak_active = 0

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "unknown"))

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: Any = None,
        tools: Any = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        request: dict[str, Any] = {
            "id": len(self.requests),
            "started": time.perf_counter(),
            "pairing_violations": verify_pairing(messages),
            "tools": [],
            "usage": None,
        }
        self.requests.append(request)
        snapshot = copy.deepcopy({"messages": messages, "system": system, "tools": tools})
        await asyncio.to_thread(save_json, self.directory / f"{request['id']:03d}.json", snapshot)
        self.active += 1
        self.peak_active = max(self.active, self.peak_active)
        try:
            async for chunk in self.inner.stream(
                messages,
                system,
                tools,
                max_tokens,
                temperature,
                **kwargs,
            ):
                if isinstance(chunk, StreamToolUse):
                    request["tools"].append(chunk.name)
                elif isinstance(chunk, StreamEnd):
                    request["usage"] = asdict(chunk.usage)
                    request["stop_reason"] = chunk.stop_reason
                yield chunk
        except BaseException as exc:
            request["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.active -= 1
            request["elapsed_seconds"] = time.perf_counter() - request["started"]
            await asyncio.to_thread(
                save_json,
                self.directory / f"{request['id']:03d}-metrics.json",
                request,
            )

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: Any = None,
        tools: Any = None,
    ) -> int:
        return await self.inner.count_tokens(messages, system, tools)

    def estimate_tokens(self, text: str) -> int:
        return self.inner.estimate_tokens(text)


def metered_totals(requests: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {
        key: sum((request.get("usage") or {}).get(key, 0) for request in requests)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    }
    return {
        **usage,
        "requests": len(requests),
        "prompt_tokens": usage["input_tokens"]
        + usage["cache_read_tokens"]
        + usage["cache_creation_tokens"],
        "complete": bool(requests)
        and all(
            request.get("usage") is not None
            and all(value >= 0 for value in request["usage"].values())
            and sum(
                request["usage"].get(key, 0)
                for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
            )
            > 0
            for request in requests
        ),
    }


async def collect_wave(
    workers: Iterable[Awaitable[SubagentResult]],
    on_result: Callable[[SubagentResult], None],
) -> None:
    """Record each completed member before waiting for the rest of its wave."""

    async def record(worker: Awaitable[SubagentResult]) -> None:
        result = await worker
        on_result(result)

    await asyncio.gather(*(record(worker) for worker in workers))


async def arm(
    root: Path,
    combined: dict[str, Any],
    tasks: list[dict[str, Any]],
    concurrency: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    instance = instance_from_record(combined)
    workspace = root / "repo"
    cache, python = Path(combined["cache"]), Path(combined["python"])
    await checkout(cache, instance, workspace)
    await checked(["git", "config", "user.name", "Carbide Evaluation"], cwd=workspace)
    await checked(["git", "config", "user.email", "evaluation@example.invalid"], cwd=workspace)
    await checked(["git", "config", "commit.gpgsign", "false"], cwd=workspace)
    base = await checked(["git", "rev-parse", "HEAD"], cwd=workspace)
    provider = ConcurrentMeter(AnthropicProvider(max_retries=1, timeout=60), root / "requests")
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    started = time.perf_counter()
    plan_snapshot: dict[str, Any] | None = None
    try:
        async with Agent(
            provider=provider,
            project_dir=workspace,
            tool_registry=create_default_registry(),
            enable_memory=False,
            enable_rules=False,
            enable_skills=False,
            enable_compression=False,
            close_provider=False,
            approval_callback=lambda _call, _prompt: True,
        ) as parent:
            if parent.session_store is None or parent.session_id is None:
                raise RuntimeError("Worker evaluation requires persistent session storage")
            todo = TodoService(TodoRepository(parent.session_store), parent.session_id)
            snapshot = todo.initialize()
            for task in tasks:
                snapshot = todo.add(
                    task["instance_id"],
                    description=(
                        task["problem_statement"]
                        + f"\nUse {python.as_posix()} -m pytest -q -o addopts= for focused tests. "
                        "Do not install dependencies or access the network. Do not inspect parent "
                        "directories or external evaluation artifacts. Preserve public behavior."
                    ),
                    expected_version=snapshot.plan_version,
                )
            if parent.subagent_manager is None:
                raise RuntimeError("Subagent manager was not enabled")

            def record_worker(child_result: SubagentResult) -> None:
                results.append(child_result.payload())
                save_json(root / "worker-results.json", results)
                print(
                    f"WORKER concurrency={concurrency} status={child_result.status.value}",
                    flush=True,
                )

            async with asyncio.timeout(timeout_seconds):
                # Waves prevent extra waiting Workers from claiming a task before
                # an earlier wave has integrated. No preferred item ID is passed.
                for offset in range(0, len(tasks), concurrency):
                    await collect_wave(
                        (
                            parent.subagent_manager.dispatch(
                                SubagentRequest(
                                    task="Inspect the shared Todo plan and claim one ready task. "
                                    "Implement it, verify focused tests, then return your result.",
                                    timeout_seconds=timeout_seconds,
                                )
                            )
                            for _ in tasks[offset : offset + concurrency]
                        ),
                        record_worker,
                    )
            current = todo.snapshot()
            if current is not None:
                plan_snapshot = asdict(current)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        client = getattr(provider.inner, "client", None)
        if client is not None:
            await client.close()
    elapsed = time.perf_counter() - started
    changed = (await checked(["git", "diff", "--name-only", base], cwd=workspace)).splitlines()
    files = [name for name in changed if production_path(instance, name)]
    patch = ""
    # Preserve diff context whitespace rather than using checked().
    if files:
        from evals.context_efficiency.repository_environment import command

        diff = await command(["git", "diff", "--binary", base, "--", *files], cwd=workspace)
        if diff.returncode:
            raise RuntimeError(diff.stderr)
        patch = diff.stdout
    (root / "submission.patch").write_text(patch, encoding="utf-8")
    validation = await grade(instance, cache, python, root / "grading", patch)
    usage = metered_totals(provider.requests)
    result = {
        "concurrency": concurrency,
        "task_ids": [t["instance_id"] for t in tasks],
        "model": provider.model,
        "runtime_sha256": implementation_fingerprint(),
        "seconds": elapsed,
        "worker_results": results,
        "errors": errors,
        "usage": usage,
        "peak_overlapping_model_requests": provider.peak_active,
        "pairing_violations": sum(len(r["pairing_violations"]) for r in provider.requests),
        "changed_files": files,
        "validation": validation,
        "todo": plan_snapshot,
        "success": bool(patch)
        and validation["success"]
        and usage["complete"]
        and not errors
        and len(results) == len(tasks)
        and all(r["status"] == "completed" for r in results),
    }
    save_json(root / "result.json", result)
    print(
        f"ARM concurrency={concurrency} success={result['success']} seconds={elapsed:.1f}",
        flush=True,
    )
    return result


async def run(args: argparse.Namespace) -> None:
    root = args.output.resolve()
    if root.exists():
        raise FileExistsError("Use a fresh output directory")
    selection = json.loads(args.composite.read_text(encoding="utf-8"))
    original = json.loads(args.source.read_text(encoding="utf-8"))
    by_id = {t["instance_id"]: t for t in original["selected"]}
    tasks = [by_id[item] for item in selection["original_issue_ids"]]
    save_json(
        root / "protocol.json",
        {
            "kind": "live scheduling pilot; not single-Agent product vs multi-Agent product",
            "task_ids": selection["original_issue_ids"],
            "repetitions": 1,
            "arm_order": [1, 2],
            "timeout_seconds_per_arm": args.timeout_seconds,
            "runtime_sha256": implementation_fingerprint(),
            "limitations": "one derived maintenance workload; fixed arm order; no speedup conclusion",
        },
    )
    combined = selection["selected"][0]
    records = []
    for concurrency in (1, 2):
        records.append(
            await arm(
                root / f"concurrency-{concurrency}",
                combined,
                tasks,
                concurrency,
                args.timeout_seconds,
            )
        )
        save_json(root / "results.json", records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--composite", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("Timeout must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
