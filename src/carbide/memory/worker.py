"""Asynchronous, recoverable memory extraction worker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from carbide.memory.extractor import MemoryTurn
from carbide.memory.manager import MemoryManager, MemoryProcessResult

MemoryResultCallback = Callable[["MemoryJobResult"], Awaitable[None] | None]
TaskTracker = Callable[[asyncio.Task[Any]], None]


@dataclass(frozen=True)
class MemoryJob:
    """One extraction request, including its retry attempt."""

    job_id: str
    turn: MemoryTurn
    attempt: int = 0

    @classmethod
    def from_turn(cls, turn: MemoryTurn) -> MemoryJob:
        payload = json.dumps(
            {
                "assistant_message": turn.assistant_message,
                "session_id": turn.session_id,
                "tool_summary": turn.tool_summary,
                "turn_id": turn.turn_id,
                "user_message": turn.user_message,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return cls(job_id=hashlib.sha256(payload).hexdigest(), turn=turn)


@dataclass(frozen=True)
class MemoryJobResult:
    """Result of a background extraction job."""

    job_id: str
    turn: MemoryTurn
    result: MemoryProcessResult
    attempts: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = self.result.to_dict()
        payload.update(
            {
                "job_id": self.job_id,
                "attempts": self.attempts,
                "status": self.status,
            }
        )
        if self.error:
            payload["error"] = self.error
        return payload

    @property
    def status(self) -> str:
        if self.error or self.result.errors:
            return "failed"
        if self.result.saved or self.result.updated:
            return "saved"
        if self.result.skipped:
            return "skipped"
        if self.result.attempted:
            return "empty"
        return "ignored"


class _MemoryJobStore:
    """Small file-backed queue journal used for crash recovery."""

    MAX_JOB_BYTES = 128_000
    MAX_TEXT_CHARS = 20_000

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.failed = self.root / "failed"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.failed.mkdir(parents=True, exist_ok=True)

    def load_pending(self) -> list[MemoryJob]:
        jobs: list[MemoryJob] = []
        for path in sorted(self.pending.glob("*.json")):
            try:
                if path.is_symlink() or path.stat().st_size > self.MAX_JOB_BYTES:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = self._decode(payload)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if job is not None and job.job_id == path.stem:
                jobs.append(job)
        return jobs

    def write(self, job: MemoryJob) -> None:
        path = self.pending / f"{job.job_id}.json"
        payload = {
            "version": 1,
            "job_id": job.job_id,
            "attempt": job.attempt,
            "turn": {
                "user_message": job.turn.user_message,
                "assistant_message": job.turn.assistant_message,
                "tool_summary": job.turn.tool_summary,
                "session_id": job.turn.session_id,
                "turn_id": job.turn.turn_id,
            },
        }
        temporary = self.pending / f".{job.job_id}.{uuid.uuid4().hex}.tmp"
        self._write_atomic(temporary, path, payload)

    def remove(self, job_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.pending / f"{job_id}.json").unlink()

    def mark_failed(self, job: MemoryJob, error: str) -> None:
        payload = {
            "version": 1,
            "job_id": job.job_id,
            "attempt": job.attempt,
            "error": error[:500],
            "failed_at": time.time(),
            "turn": {
                "user_message": job.turn.user_message,
                "assistant_message": job.turn.assistant_message,
                "tool_summary": job.turn.tool_summary,
                "session_id": job.turn.session_id,
                "turn_id": job.turn.turn_id,
            },
        }
        temporary = self.failed / f".{job.job_id}.{uuid.uuid4().hex}.tmp"
        self._write_atomic(temporary, self.failed / f"{job.job_id}.json", payload)
        self.remove(job.job_id)

    @staticmethod
    def _write_atomic(temporary: Path, destination: Path, payload: dict[str, Any]) -> None:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)

    @classmethod
    def _decode(cls, payload: Any) -> MemoryJob | None:
        if not isinstance(payload, dict):
            return None
        job_id = payload.get("job_id")
        attempt = payload.get("attempt", 0)
        turn = payload.get("turn")
        if not isinstance(job_id, str) or not isinstance(attempt, int) or attempt < 0:
            return None
        if not isinstance(turn, dict):
            return None
        user_message = turn.get("user_message")
        assistant_message = turn.get("assistant_message")
        tool_summary = turn.get("tool_summary", "")
        session_id = turn.get("session_id")
        turn_id = turn.get("turn_id")
        if not isinstance(user_message, str) or len(user_message) > cls.MAX_TEXT_CHARS:
            return None
        if not isinstance(assistant_message, str) or len(assistant_message) > cls.MAX_TEXT_CHARS:
            return None
        if not isinstance(tool_summary, str) or len(tool_summary) > cls.MAX_TEXT_CHARS:
            return None
        if session_id is not None and not isinstance(session_id, str):
            return None
        if turn_id is not None and not isinstance(turn_id, int):
            return None
        return MemoryJob(
            job_id=job_id,
            attempt=attempt,
            turn=MemoryTurn(
                user_message=user_message,
                assistant_message=assistant_message,
                tool_summary=tool_summary,
                session_id=session_id,
                turn_id=turn_id,
            ),
        )


class MemoryWorker:
    """Run memory extraction outside the main response path.

    Jobs are journaled before entering the in-process queue.  A graceful
    shutdown drains the queue; an ungraceful shutdown leaves pending files for
    the next Agent instance to recover.
    """

    def __init__(
        self,
        manager: MemoryManager,
        *,
        queue_dir: Path | None = None,
        max_queue_size: int = 128,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        result_callback: MemoryResultCallback | None = None,
        task_tracker: TaskTracker | None = None,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must not be negative")
        self.manager = manager
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.result_callback = result_callback
        self.task_tracker = task_tracker
        self._store = _MemoryJobStore(queue_dir or manager.memory_dir / ".queue")
        self._queue: asyncio.Queue[MemoryJob | None] = asyncio.Queue(maxsize=max_queue_size)
        self._start_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._recovered_jobs = self._store.load_pending()
        self._known_jobs = {job.job_id for job in self._recovered_jobs}
        self._results: deque[MemoryJobResult] = deque(maxlen=256)
        self._closed = False
        self._stats = {"submitted": 0, "completed": 0, "retried": 0, "failed": 0}

    async def start(self) -> None:
        """Start the worker and enqueue jobs recovered from a prior run."""
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("memory worker is closed")
            if self.manager.mode.value == "off":
                return
            if self._task is not None and not self._task.done():
                return
            self._task = asyncio.create_task(self._run(), name="memory-worker")
            if self.task_tracker is not None:
                self.task_tracker(self._task)
            for job in self._recovered_jobs:
                await self._queue.put(job)
            self._recovered_jobs.clear()

    async def submit(self, turn: MemoryTurn) -> MemoryJob | None:
        """Journal and enqueue a turn when policy says it merits extraction."""
        if not self.manager.should_process(turn.user_message):
            return None
        if self._closed:
            raise RuntimeError("memory worker is closed")
        job = MemoryJob.from_turn(turn)
        await self.start()
        if job.job_id in self._known_jobs:
            return job
        self._store.write(job)
        self._known_jobs.add(job.job_id)
        await self._queue.put(job)
        self._stats["submitted"] += 1
        return job

    async def drain(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Wait until all journaled jobs currently in the queue are handled."""
        if self._task is None and self._recovered_jobs:
            await self.start()
        if self._task is None or self._task.done():
            return
        join = self._queue.join()
        if timeout is None:
            await join
        else:
            await asyncio.wait_for(join, timeout=max(0.0, timeout))

    async def close(self, timeout: float = 35.0) -> None:  # noqa: ASYNC109
        """Drain and stop the worker without cancelling unrelated tasks."""
        if self._closed:
            return
        self._closed = True
        if self._task is None and self._recovered_jobs:
            self._closed = False
            await self.start()
            self._closed = True
        task = self._task
        if task is not None and task.done():
            self._task = None
            task = None
        if task is None and self._recovered_jobs:
            self._closed = False
            await self.start()
            self._closed = True
            task = self._task
        if task is None:
            return

        started = asyncio.get_running_loop().time()
        try:
            await self.drain(timeout=timeout)
            remaining = max(0.0, timeout - (asyncio.get_running_loop().time() - started))
            await self._queue.put(None)
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._task = None

    def pop_results(self) -> list[MemoryJobResult]:
        """Return completed results for a UI or host application to render."""
        results = list(self._results)
        self._results.clear()
        return results

    def get_stats(self) -> dict[str, int]:
        return {
            **self._stats,
            "pending": self._queue.qsize() + len(self._recovered_jobs),
            "failed_jobs": len(list(self._store.failed.glob("*.json"))),
        }

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                with contextlib.suppress(Exception):
                    self._store.mark_failed(job, error)
                self._known_jobs.discard(job.job_id)
                self._stats["failed"] += 1
                result = MemoryProcessResult(attempted=True, errors=[error])
                record = MemoryJobResult(
                    job_id=job.job_id,
                    turn=job.turn,
                    result=result,
                    attempts=job.attempt + 1,
                    error=error,
                )
                await self._publish_result(record)
            finally:
                self._queue.task_done()

    async def _process(self, job: MemoryJob) -> None:
        try:
            result = await self.manager.process_turn(job.turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = MemoryProcessResult(attempted=True, errors=[str(exc) or type(exc).__name__])

        if result.errors and job.attempt < self.max_retries:
            retry = replace(job, attempt=job.attempt + 1)
            self._store.write(retry)
            self._stats["retried"] += 1
            if self.retry_backoff:
                await asyncio.sleep(self.retry_backoff * (2**job.attempt))
            await self._queue.put(retry)
            return

        error = "; ".join(result.errors) if result.errors else None
        if error:
            self._store.mark_failed(job, error)
            self._stats["failed"] += 1
        else:
            self._store.remove(job.job_id)
            self._stats["completed"] += 1
        self._known_jobs.discard(job.job_id)
        record = MemoryJobResult(
            job_id=job.job_id,
            turn=job.turn,
            result=result,
            attempts=job.attempt + 1,
            error=error,
        )
        await self._publish_result(record)

    async def _publish_result(self, record: MemoryJobResult) -> None:
        self._results.append(record)
        if self.result_callback is not None:
            with contextlib.suppress(Exception):
                callback_result = self.result_callback(record)
                if inspect.isawaitable(callback_result):
                    await callback_result


__all__ = ["MemoryJob", "MemoryJobResult", "MemoryWorker"]
