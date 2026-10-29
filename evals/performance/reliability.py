"""Repeatable fault injection against real SQLite, Agent and Git components.

Provider doubles intentionally isolate runtime reliability from model quality.
The mailbox ACK-gap scenario is a capability probe, not an assumed guarantee.
Raw failures are retained and cause a nonzero exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import multiprocessing
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.agents.worktree import WorktreeManager
from carbide.engine.repair import verify_pairing
from carbide.governance.session_store import SessionStore
from carbide.providers.base import (
    Provider,
    ProviderError,
    StreamEnd,
    StreamTextDelta,
    StreamToolUse,
)
from carbide.sdk import Agent
from carbide.todo.errors import TodoConflictError
from carbide.todo.repository import TodoRepository
from carbide.todo.service import TodoService
from carbide.tools.defaults import create_default_registry
from evals.context_efficiency.repository_ab import implementation_fingerprint
from evals.context_efficiency.repository_environment import command, save_json

Details = dict[str, Any]
Scenario = Callable[[Path], Awaitable[Details]]
_claim_barrier: Any = None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class RuntimeProvider(Provider):
    """Deterministic final response, or a stream interrupted after dispatch."""

    def __init__(self, *, interrupted: bool = False) -> None:
        self.interrupted = interrupted

    @property
    def model(self) -> str:
        return "fault-injection-not-a-real-model"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: Any = None,
        tools: Any = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        if self.interrupted:
            yield StreamToolUse("interrupted-read", "read", {"path": "app.py"})
            raise ProviderError("injected disconnect after tool dispatch")
        yield StreamTextDelta("recovered")
        yield StreamEnd()

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: Any = None,
        tools: Any = None,
    ) -> int:
        return 1

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def make_agent(root: Path, **kwargs: Any) -> Agent:
    return Agent(
        project_dir=root,
        tool_registry=create_default_registry(),
        enable_memory=False,
        enable_rules=False,
        enable_skills=False,
        enable_compression=False,
        enable_subagents=False,
        **kwargs,
    )


async def resume(root: Path, session: str) -> Details:
    start = time.perf_counter()
    async with make_agent(root, provider=RuntimeProvider(), resume_session_id=session) as agent:
        require(not verify_pairing(agent.loop.state.messages), "unpaired resumed history")
        events = [event async for event in agent.query("continue after interruption")]
        require(
            any(e.type == "chunk" and e.data.content == "recovered" for e in events),
            "resumed query did not complete",
        )
        require(not any(e.type == "error" for e in events), "resume raised an error")
        require(not verify_pairing(agent.loop.state.messages), "unpaired continuation")
    with SessionStore(root / ".carbide" / "sessions.db") as store:
        messages = store.load_messages(session)
        require(not verify_pairing(messages), "persisted history is unpaired")
    return {"resume_ms": (time.perf_counter() - start) * 1000, "messages": len(messages)}


async def provider_disconnect(root: Path) -> Details:
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    async with make_agent(root, provider=RuntimeProvider(interrupted=True)) as agent:
        session = agent.session_id
        events = [event async for event in agent.query("read app.py")]
        require(any(e.type == "error" for e in events), "fault did not surface")
        require(any(e.type == "tool_complete" for e in events), "dispatched tool was lost")
        require(not verify_pairing(agent.loop.state.messages), "interrupted history unpaired")
    if session is None:
        raise AssertionError("missing session id")
    return await resume(root, session)


def crash_worker(root: Path) -> None:
    """Exit without connection close after committing a tool_use, before result."""
    store = SessionStore(root / ".carbide" / "sessions.db")
    store.open()
    store.create_session("crash-session", root)
    store.append_message("crash-session", "user", [{"type": "text", "text": "read app.py"}])
    store.append_message(
        "crash-session",
        "assistant",
        [{"type": "tool_use", "id": "crash-read", "name": "read", "input": {"path": "app.py"}}],
    )
    os._exit(23)


async def process_crash(root: Path) -> Details:
    result = await command(
        [sys.executable, "-m", "evals.performance.reliability", "--crash-worker", str(root)],
        timeout_seconds=30,
    )
    require(result.returncode == 23, f"crash injection failed: {result.stderr}")
    with SessionStore(root / ".carbide" / "sessions.db") as store:
        require(
            bool(verify_pairing(store.load_messages("crash-session"))),
            "injection did not leave an orphan tool call",
        )
    return {"crash_exit_code": result.returncode, **await resume(root, "crash-session")}


def initialize_plan(path: Path, root: Path) -> str:
    with SessionStore(path) as store:
        store.create_session("parent", root)
        service = TodoService(TodoRepository(store), "parent")
        service.initialize()
        snapshot = service.add("one exclusive task", expected_version=0)
        return snapshot.items[0].id


def initialize_contender(barrier: Any) -> None:
    global _claim_barrier
    _claim_barrier = barrier


def claim_worker(path: str, index: int) -> Details:
    with SessionStore(Path(path)) as store:
        service = TodoService(TodoRepository(store), "parent", actor_id=f"worker-{index}")
        _claim_barrier.wait(timeout=30)
        started = time.perf_counter()
        claim = service.claim_next(f"worker-{index}", lease_seconds=120)
        return {
            "worker": index,
            "pid": os.getpid(),
            "item_id": claim.item.id if claim else None,
            "claim_ms": (time.perf_counter() - started) * 1000,
        }


def contend(path: Path, workers: int) -> list[Details]:
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=context,
        initializer=initialize_contender,
        initargs=(context.Barrier(workers),),
    ) as pool:
        futures = [pool.submit(claim_worker, str(path), i) for i in range(workers)]
        return [future.result(timeout=45) for future in futures]


async def exclusive_claim(root: Path) -> Details:
    path = root / "claims.db"
    item_id = initialize_plan(path, root)
    results = await asyncio.to_thread(contend, path, 8)
    require(len({r["pid"] for r in results}) == 8, "not eight distinct OS processes")
    winners = [r for r in results if r["item_id"] is not None]
    require(
        len(winners) == 1 and winners[0]["item_id"] == item_id,
        f"task was not claimed exactly once: {results}",
    )
    return {"contenders": 8, "winners": len(winners), "workers": results}


async def stale_owner(root: Path) -> Details:
    path = root / "leases.db"
    item_id = initialize_plan(path, root)
    with SessionStore(path) as store:
        service = TodoService(TodoRepository(store), "parent")
        old = service.claim_next("old-owner", lease_seconds=0.05)
        require(old is not None, "initial claim failed")
        store.create_agent_run(
            run_id="child",
            parent_session_id="parent",
            child_session_id=None,
            mode="worker",
            role=None,
            task="task",
            background=True,
            owner_id="old-owner",
            lease_seconds=0.05,
        )
        await asyncio.sleep(0.08)
        started = time.perf_counter()
        reaped = store.reap_expired_agent_runs(parent_session_id="parent")
        require(reaped == ["child"], "expired run was not reaped")
        current = service.claim_next("new-owner", lease_seconds=30)
        require(current is not None, "expired task was not reclaimable")
        rejected = 0
        try:
            service.update(item_id, status="completed", owner_id="old-owner")
        except TodoConflictError:
            rejected += 1
        require(
            not store.update_agent_run("child", status="completed", owner_id="old-owner"),
            "orphaned owner updated run",
        )
        rejected += 1
        try:
            store.append_agent_message(
                run_id="child",
                direction="child_to_parent",
                kind="result",
                content="stale",
                owner_id="old-owner",
            )
        except ValueError:
            rejected += 1
        require(rejected == 3, "stale owner message or Todo write was accepted")
        if current is None:
            raise AssertionError("missing reclaimed task")
        snapshot = service.update(
            item_id,
            status="completed",
            owner_id="new-owner",
            expected_revision=current.item.revision,
        )
        require(snapshot.item_map[item_id].status.value == "completed", "new owner failed")
        return {
            "rejected_stale_writes": rejected,
            "lease_seconds": 0.05,
            "reclaim_and_fence_ms": (time.perf_counter() - started) * 1000,
        }


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def initialize_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Carbide Evaluation")
    git(repository, "config", "user.email", "evaluation@example.invalid")
    git(repository, "config", "commit.gpgsign", "false")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "app.py")
    git(repository, "commit", "-m", "base fixture")
    return repository


async def worktrees(root: Path, *, conflict: bool) -> Details:
    repository = await asyncio.to_thread(initialize_repository, root)
    manager = WorktreeManager(repository)
    first = await manager.prepare("first")
    second = await manager.prepare("second")
    try:
        if conflict:
            (first.workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            (second.workspace / "app.py").write_text("value = 3\n", encoding="utf-8")
        else:
            (first.workspace / "first.py").write_text("first = True\n", encoding="utf-8")
            (second.workspace / "second.py").write_text("second = True\n", encoding="utf-8")
        outcomes = await asyncio.gather(
            manager.finalize(first, commit_message="first worker"),
            manager.finalize(second, commit_message="second worker"),
        )
        statuses = sorted(item.status for item in outcomes)
        require(
            statuses == (["merge_blocked", "merged"] if conflict else ["merged", "merged"]),
            f"unexpected integration outcome: {outcomes}",
        )
        require(git(repository, "status", "--porcelain") == "", "parent left dirty")
        require(not first.path.exists() and not second.path.exists(), "worktree leak")
        gitdir = repository / ".git"
        require(
            not any(
                (gitdir / name).exists() for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply")
            ),
            "parent left in partial integration state",
        )
        if conflict:
            value = (repository / "app.py").read_text(encoding="utf-8")
            require(value in {"value = 2\n", "value = 3\n"}, "parent content corrupted")
        else:
            require(
                (repository / "first.py").read_text(encoding="utf-8") == "first = True\n",
                "first patch lost",
            )
            require(
                (repository / "second.py").read_text(encoding="utf-8") == "second = True\n",
                "second patch lost",
            )
        return {"statuses": statuses, "parent_clean": True, "worktrees_removed": 2}
    finally:
        for handle in (first, second):
            if handle.path.exists():
                await manager.cleanup(handle)


async def worktree_conflict(root: Path) -> Details:
    return await worktrees(root, conflict=True)


async def worktree_disjoint(root: Path) -> Details:
    return await worktrees(root, conflict=False)


async def mailbox_ack_gap(root: Path) -> Details:
    path = root / "mailbox.db"
    with SessionStore(path) as store:
        store.create_session("parent", root)
        store.create_agent_run(
            run_id="child",
            parent_session_id="parent",
            child_session_id=None,
            mode="worker",
            role=None,
            task="task",
            background=True,
        )
        message_id = store.append_agent_message(
            run_id="child",
            direction="child_to_parent",
            kind="result",
            content="full child result",
        )
    # Queued message survives a new connection/process lifecycle.
    with SessionStore(path) as store:
        claimed = store.claim_agent_messages_for_parent(parent_session_id="parent")
        require(
        len(claimed) == 1 and claimed[0]["id"] == message_id,
            "queued message did not survive reopen",
        )
    # Simulate consumer dying after durable delivery, before ACK/context injection.
    with SessionStore(path) as store:
        redelivered = store.claim_agent_messages_for_parent(parent_session_id="parent")
        with store.connection() as conn:
            status = conn.execute(
                "SELECT status FROM agent_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()[0]
    return {
        "queued_survived_reopen": True,
        "status_after_consumer_restart": status,
        "unacked_redelivered": any(m["id"] == message_id for m in redelivered),
        "capability_gap": not redelivered,
    }


SCENARIOS: dict[str, Scenario] = {
    "provider_disconnect_resume": provider_disconnect,
    "abrupt_process_exit_resume": process_crash,
    "eight_process_exclusive_claim": exclusive_claim,
    "expired_owner_fencing": stale_owner,
    "concurrent_worktree_disjoint": worktree_disjoint,
    "concurrent_worktree_conflict": worktree_conflict,
    "mailbox_unacked_redelivery": mailbox_ack_gap,
}


def summarize(records: list[Details]) -> Details:
    summary: Details = {}
    for name in SCENARIOS:
        rows = [r for r in records if r["scenario"] == name]
        if not rows:
            continue
        values = sorted(float(r["elapsed_ms"]) for r in rows)
        summary[name] = {
            "trials": len(rows),
            "passed": sum(r["passed"] for r in rows),
            "failed": sum(not r["passed"] for r in rows),
            "p50_ms": values[math.ceil(len(values) * 0.5) - 1],
            "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
        }
    return summary


async def run_trial(name: str, root: Path, repeat: int, scenario: Scenario) -> Details:
    started = time.perf_counter()
    record: Details = {"scenario": name, "repeat": repeat, "passed": False}
    try:
        await asyncio.to_thread(root.mkdir, parents=True)
        record["details"] = await scenario(root)
        record["passed"] = not record["details"].get("capability_gap", False)
        if not record["passed"]:
            record["error"] = "Capability gap: delivered-but-unacknowledged message not redelivered"
    except Exception:
        record["error"] = traceback.format_exc()
    record["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return record


async def evaluate(output: Path, repetitions: int, names: list[str]) -> bool:
    if await asyncio.to_thread(output.exists):
        raise FileExistsError(f"Refusing to overwrite results: {output}")
    metadata = {
        "started_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_sha256": implementation_fingerprint(),
        "evaluator_sha256": hashlib.sha256(
            await asyncio.to_thread(Path(__file__).read_bytes)
        ).hexdigest(),
        "repetitions": repetitions,
        "scenarios": names,
        "model": "deterministic provider double; no API calls",
    }
    records: list[Details] = []
    save_json(output / "environment.json", metadata)
    with tempfile.TemporaryDirectory(prefix="carbide-reliability-") as temporary:
        for repeat in range(1, repetitions + 1):
            for name in names:
                record = await run_trial(
                    name, Path(temporary) / f"{repeat}-{name}", repeat, SCENARIOS[name]
                )
                records.append(record)
                save_json(output / "trials" / f"{repeat:03d}-{name}.json", record)
                save_json(output / "summary.json", summarize(records))
                print(
                    f"{repeat}/{repetitions} {name}: {'PASS' if record['passed'] else 'FAIL'}",
                    flush=True,
                )
    save_json(
        output / "results.json",
        {"environment": metadata, "records": records, "summary": summarize(records)},
    )
    return all(r["passed"] for r in records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--crash-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_worker is not None:
        crash_worker(args.crash_worker)
        return
    if args.output is None or args.repetitions < 1:
        parser.error("--output and positive --repetitions are required")
    passed = asyncio.run(evaluate(args.output.resolve(), args.repetitions, args.scenarios))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
