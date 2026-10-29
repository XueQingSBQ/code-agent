"""Domain objects for managed background processes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

JobStatus = Literal["running", "stopped", "failed", "unknown"]


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class BackgroundJob:
    """Persisted identity and lifecycle state for one spawned process."""

    job_id: str
    pid: int
    command: str
    workspace: Path
    owner_session_id: str
    status: JobStatus
    started_at: datetime
    detached: bool = False
    backend: str | None = None
    log_path: Path | None = None
    exit_code: int | None = None
    stopped_at: datetime | None = None
    last_checked_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "job_id": self.job_id,
            "pid": self.pid,
            "command": self.command,
            "workspace": str(self.workspace),
            "owner_session_id": self.owner_session_id,
            "status": self.status,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "detached": self.detached,
            "backend": self.backend,
            "log_path": str(self.log_path) if self.log_path is not None else None,
            "exit_code": self.exit_code,
            "stopped_at": self.stopped_at.astimezone(UTC).isoformat()
            if self.stopped_at is not None
            else None,
            "last_checked_at": self.last_checked_at.astimezone(UTC).isoformat()
            if self.last_checked_at is not None
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackgroundJob:
        """Validate and hydrate metadata loaded from disk."""
        status = data.get("status")
        if status not in {"running", "stopped", "failed", "unknown"}:
            raise ValueError(f"invalid background job status: {status!r}")

        log_value = data.get("log_path")
        stopped_value = data.get("stopped_at")
        checked_value = data.get("last_checked_at")
        return cls(
            job_id=str(data["job_id"]),
            pid=int(data["pid"]),
            command=str(data["command"]),
            workspace=Path(str(data["workspace"])),
            owner_session_id=str(data["owner_session_id"]),
            status=status,
            started_at=_parse_datetime(data["started_at"], field="started_at"),
            detached=bool(data.get("detached", False)),
            backend=str(data["backend"]) if data.get("backend") else None,
            log_path=Path(str(log_value)) if log_value else None,
            exit_code=int(data["exit_code"]) if data.get("exit_code") is not None else None,
            stopped_at=(
                _parse_datetime(stopped_value, field="stopped_at") if stopped_value else None
            ),
            last_checked_at=(
                _parse_datetime(checked_value, field="last_checked_at") if checked_value else None
            ),
        )
