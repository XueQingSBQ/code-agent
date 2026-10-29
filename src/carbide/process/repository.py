"""Durable, atomic storage for background-job metadata."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from carbide.process.domain import BackgroundJob
from carbide.process.errors import JobNotFoundError, JobStoreError


class JobRepository:
    """Store one validated JSON document per job.

    A per-job file keeps recovery independent from the session SQLite database:
    a crashed agent can still be inspected and cleaned up without opening the
    conversation store. Writes use a same-directory temporary file followed by
    ``Path.replace`` so readers never observe a partial document.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        if not job_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in job_id
        ):
            raise JobStoreError(f"invalid job id: {job_id!r}")
        return self.root / f"{job_id}.json"

    def save(self, job: BackgroundJob) -> None:
        destination = self._path(job.job_id)
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=f".{job.job_id}.", suffix=".tmp", dir=self.root)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(job.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
        except (OSError, TypeError, ValueError) as exc:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            raise JobStoreError(f"could not persist job {job.job_id}: {exc}") from exc

    def get(self, job_id: str) -> BackgroundJob:
        path = self._path(job_id)
        if not path.exists():
            raise JobNotFoundError(f"background job not found: {job_id}")
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("metadata root must be an object")
            return BackgroundJob.from_dict(data)
        except JobNotFoundError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise JobStoreError(f"could not load job {job_id}: {exc}") from exc

    def list(self, *, owner_session_id: str | None = None) -> list[BackgroundJob]:
        jobs: list[BackgroundJob] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    raise ValueError("metadata root must be an object")
                job = BackgroundJob.from_dict(data)
            except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                raise JobStoreError(f"could not load job metadata {path.name}: {exc}") from exc
            if owner_session_id is None or job.owner_session_id == owner_session_id:
                jobs.append(job)
        return sorted(jobs, key=lambda job: job.started_at, reverse=True)
