"""Tests for audit logging system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carbide.governance import AuditLogger


def test_audit_logger_creates_log_file(tmp_path: Path) -> None:
    """AuditLogger creates the log file and parent directories."""
    log_path = tmp_path / "logs" / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test-session")

    logger.open()
    logger.close()

    assert log_path.exists()


def test_audit_logger_writes_jsonl_format(tmp_path: Path) -> None:
    """Each entry is written as a single JSON line."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test-session")

    logger.open()
    logger.log_permission_check("bash", {"command": "ls"}, "approved")
    logger.log_tool_call("bash", {"command": "ls"})
    logger.close()

    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2

    # Each line is valid JSON
    for line in lines:
        entry = json.loads(line)
        assert "timestamp" in entry
        assert "session_id" in entry
        assert entry["session_id"] == "test-session"


def test_audit_logger_is_append_only(tmp_path: Path) -> None:
    """Opening the logger multiple times appends, never overwrites."""
    log_path = tmp_path / "audit.jsonl"

    # First session
    logger1 = AuditLogger(log_path, session_id="session-1")
    logger1.open()
    logger1.log_tool_call("read", {"path": "a.py"})
    logger1.close()

    # Second session
    logger2 = AuditLogger(log_path, session_id="session-2")
    logger2.open()
    logger2.log_tool_call("write", {"path": "b.py"})
    logger2.close()

    # Both entries are preserved
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2

    entries = [json.loads(line) for line in lines]
    assert entries[0]["session_id"] == "session-1"
    assert entries[1]["session_id"] == "session-2"


def test_audit_logger_flushes_immediately(tmp_path: Path) -> None:
    """Entries are readable immediately after log() call."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test")

    logger.open()
    logger.log_tool_call("bash", {"command": "echo hello"})

    # Read without closing the logger
    content = log_path.read_text(encoding="utf-8")
    assert "bash" in content
    assert "echo hello" in content

    logger.close()


def test_audit_entry_includes_all_fields(tmp_path: Path) -> None:
    """AuditEntry contains all required fields."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test")

    logger.open()
    logger.log_tool_result("bash", "success", is_error=False, duration_ms=123.45)
    logger.close()

    line = log_path.read_text(encoding="utf-8").strip()
    entry = json.loads(line)

    assert entry["event_type"] == "tool_result"
    assert entry["tool_name"] == "bash"
    assert entry["result"] == "success"
    assert entry["is_error"] is False
    assert entry["duration_ms"] == 123.45


def test_audit_logger_context_manager(tmp_path: Path) -> None:
    """AuditLogger works as a context manager."""
    log_path = tmp_path / "audit.jsonl"

    with AuditLogger(log_path, session_id="test") as logger:
        logger.log_tool_call("read", {"path": "file.txt"})

    # File is closed and readable
    content = log_path.read_text(encoding="utf-8")
    assert "read" in content


def test_audit_logger_logs_errors(tmp_path: Path) -> None:
    """Error events are properly logged."""
    log_path = tmp_path / "audit.jsonl"

    with AuditLogger(log_path, session_id="test") as logger:
        logger.log_error("bash", "Command failed: not found")

    line = log_path.read_text(encoding="utf-8").strip()
    entry = json.loads(line)

    assert entry["event_type"] == "error"
    assert entry["tool_name"] == "bash"
    assert entry["is_error"] is True
    assert "not found" in entry["result"]


def test_audit_logger_raises_if_not_opened(tmp_path: Path) -> None:
    """Logging without opening raises RuntimeError."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test")

    with pytest.raises(RuntimeError, match="not opened"):
        logger.log_tool_call("bash", {})


def test_audit_logger_redacts_secrets_and_truncates_large_values(tmp_path: Path) -> None:
    """Credentials and oversized tool output must not be persisted verbatim."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test", max_field_chars=128)

    with logger:
        logger.log_tool_call(
            "bash",
            {
                "api_key": "sk-secret-value",
                "nested": {"password": "correct horse"},
                "command": "echo token=inline-secret Bearer abc123 " + ("x" * 200),
            },
        )

    content = log_path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in content
    assert "correct horse" not in content
    assert "inline-secret" not in content
    assert "abc123" not in content
    assert "[REDACTED]" in content
    assert "[TRUNCATED]" in content


def test_audit_logger_rotates_before_file_exceeds_limit(tmp_path: Path) -> None:
    """Rotation keeps numbered backups valid JSONL and bounds the active file."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, session_id="test", max_bytes=350, backup_count=2)

    with logger:
        for index in range(8):
            logger.log_tool_call("read", {"path": f"file-{index}.py", "value": "x" * 80})

    rotated = sorted(tmp_path.glob("audit.jsonl.*"))
    assert rotated
    assert len(rotated) <= 2
    assert log_path.stat().st_size <= 350
    for path in [log_path, *rotated]:
        for line in path.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def test_audit_logger_rejects_invalid_rotation_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        AuditLogger(tmp_path / "audit.jsonl", "test", max_bytes=0)
    with pytest.raises(ValueError, match="backup_count"):
        AuditLogger(tmp_path / "audit.jsonl", "test", backup_count=-1)
    with pytest.raises(ValueError, match="max_field_chars"):
        AuditLogger(tmp_path / "audit.jsonl", "test", max_field_chars=1)
