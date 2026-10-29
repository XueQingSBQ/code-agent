"""Audit logging for governance layer.

M2 governance layer: append-only JSONL audit log for tracking all tool operations,
permission decisions, and errors. Each entry is a single JSON line with timestamp,
session ID, event type, and relevant metadata.

The audit log is designed for:
- Compliance requirements (who did what when)
- Debugging (trace tool execution flow)
- Security analysis (detect anomalous patterns)
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|auth(?:orization)?|password|passwd|secret|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_INLINE_SECRET_RE = re.compile(
    r"(?P<key>api[_-]?key|token|access[_-]?token|refresh[_-]?token|password|passwd|secret)"
    r"(?P<separator>\s*[=:]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?P<prefix>\bBearer\s+)(?P<value>[^\s,;]+)", re.IGNORECASE)
_REDACTED = "[REDACTED]"
_TRUNCATED = "...[TRUNCATED]"


@dataclass
class AuditEntry:
    """Single audit log entry."""

    timestamp: str  # ISO 8601 UTC
    session_id: str
    event_type: str  # "tool_call", "tool_result", "permission_check", "error"
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: str | None = None
    is_error: bool = False
    duration_ms: float | None = None
    permission_decision: str | None = None  # "approved", "denied", or None


class AuditLogger:
    """Append-only JSONL audit logger with bounded, redacted output.

    Audit records are useful for incident analysis, but tool arguments and
    results can contain credentials or unbounded command output.  The logger
    therefore sanitizes values before serialization and rotates the active
    file before it exceeds ``max_bytes``.  Rotation and writes are protected by
    one process-local lock, which also keeps concurrent tool execution from
    interleaving JSON lines.
    """

    def __init__(
        self,
        log_path: Path,
        session_id: str,
        *,
        max_bytes: int = 10_000_000,
        backup_count: int = 5,
        max_field_chars: int = 16_384,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count must be non-negative")
        if max_field_chars < 128:
            raise ValueError("max_field_chars must be at least 128")
        self._log_path = log_path
        self._session_id = session_id
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._max_field_chars = max_field_chars
        self._opened = False
        self._lock = threading.RLock()

    def open(self) -> None:
        """Open the log file for appending."""
        with self._lock:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            # Do not retain an open handle between events. On Windows a
            # forgotten Agent context would otherwise keep a temporary
            # workspace undeletable until process exit.
            self._log_path.touch(exist_ok=True)
            self._opened = True

    def log(self, entry: AuditEntry) -> None:
        """Write an entry to the log and flush immediately."""
        if not self._opened:
            raise RuntimeError("AuditLogger not opened")

        sanitized = self._sanitize_value(asdict(entry))
        line = json.dumps(sanitized, ensure_ascii=False, default=str) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with self._log_path.open("ab") as log_file:
                log_file.write(encoded)
                log_file.flush()  # Immediate flush for crash safety
                # fsync is deliberately not unconditional: it makes every
                # tool event pay a disk-sync cost. flush() gives the existing
                # API its crash-visible behavior; callers can use filesystem
                # journaling/backup for stronger durability.

    def _sanitize_value(self, value: Any, *, key: str | None = None, depth: int = 0) -> Any:
        """Return a JSON-safe, bounded copy with likely secrets removed."""
        if key is not None and _SECRET_KEY_RE.search(key):
            return _REDACTED
        if isinstance(value, str):
            sanitized = _BEARER_RE.sub(lambda match: match.group("prefix") + _REDACTED, value)
            sanitized = _INLINE_SECRET_RE.sub(
                lambda match: match.group("key") + match.group("separator") + _REDACTED,
                sanitized,
            )
            if len(sanitized) > self._max_field_chars:
                sanitized = sanitized[: self._max_field_chars] + _TRUNCATED
            return sanitized
        if depth >= 8:
            return "[MAX_DEPTH]"
        if isinstance(value, dict):
            return {
                str(child_key): self._sanitize_value(
                    child_value, key=str(child_key), depth=depth + 1
                )
                for child_key, child_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize_value(item, depth=depth + 1) for item in value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return self._sanitize_value(str(value), depth=depth + 1)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        """Rotate old files while holding ``_lock`` before appending."""
        if self._backup_count == 0 or not self._log_path.exists():
            return
        try:
            current_size = self._log_path.stat().st_size
        except OSError:
            return
        if current_size == 0 or current_size + incoming_bytes <= self._max_bytes:
            return

        oldest = self._log_path.with_name(f"{self._log_path.name}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            source = self._log_path.with_name(f"{self._log_path.name}.{index}")
            destination = self._log_path.with_name(f"{self._log_path.name}.{index + 1}")
            if source.exists():
                source.replace(destination)
        self._log_path.replace(self._log_path.with_name(f"{self._log_path.name}.1"))
        self._log_path.touch(exist_ok=True)

    def log_permission_check(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: str,
    ) -> None:
        """Log a permission check decision."""
        self.log(
            AuditEntry(
                timestamp=datetime.now(UTC).isoformat(),
                session_id=self._session_id,
                event_type="permission_check",
                tool_name=tool_name,
                args=args,
                permission_decision=decision,
            )
        )

    def log_tool_call(self, tool_name: str, args: dict[str, Any]) -> None:
        """Log the start of a tool execution."""
        self.log(
            AuditEntry(
                timestamp=datetime.now(UTC).isoformat(),
                session_id=self._session_id,
                event_type="tool_call",
                tool_name=tool_name,
                args=args,
            )
        )

    def log_tool_result(
        self,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: float,
    ) -> None:
        """Log the completion of a tool execution."""
        self.log(
            AuditEntry(
                timestamp=datetime.now(UTC).isoformat(),
                session_id=self._session_id,
                event_type="tool_result",
                tool_name=tool_name,
                result=result,
                is_error=is_error,
                duration_ms=duration_ms,
            )
        )

    def log_error(self, tool_name: str, error: str) -> None:
        """Log a tool execution error."""
        self.log(
            AuditEntry(
                timestamp=datetime.now(UTC).isoformat(),
                session_id=self._session_id,
                event_type="error",
                tool_name=tool_name,
                result=error,
                is_error=True,
            )
        )

    def close(self) -> None:
        """Close the log file."""
        with self._lock:
            self._opened = False

    def __enter__(self) -> AuditLogger:
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
