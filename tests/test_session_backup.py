"""Backup, integrity, and restore tests for durable session storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from rich.console import Console

from carbide.cli.app import backup_sessions, check_sessions, migrate_sessions, restore_sessions
from carbide.governance import SessionStore
from carbide.governance.session_store import SCHEMA_VERSION


def _seed_store(path: Path) -> None:
    with SessionStore(path) as store:
        store.create_session("session-1", Path("/workspace"))
        store.append_message("session-1", "user", [{"type": "text", "text": "hello"}])


def test_session_store_integrity_backup_and_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    backup_path = tmp_path / "backups" / "sessions.db"
    restored_path = tmp_path / "restored" / "sessions.db"
    _seed_store(db_path)

    with SessionStore(db_path) as store:
        assert store.integrity_check() == "ok"
        assert store.schema_version() == SCHEMA_VERSION
        assert store.backup(backup_path) == backup_path
        with pytest.raises(FileExistsError):
            store.backup(backup_path)

    assert backup_path.exists()
    assert SessionStore.restore_backup(backup_path, restored_path) == restored_path
    with SessionStore(restored_path) as restored:
        assert restored.integrity_check() == "ok"
        assert restored.load_messages("session-1")[0]["content"][0]["text"] == "hello"


def test_restore_rejects_invalid_or_existing_targets(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    backup_path = tmp_path / "backup.db"
    _seed_store(db_path)
    backup_path.write_bytes(b"not sqlite")

    with pytest.raises(ValueError, match="Invalid session backup"):
        SessionStore.restore_backup(backup_path, tmp_path / "restored.db")
    with pytest.raises(FileNotFoundError, match="not found"):
        SessionStore.restore_backup(tmp_path / "missing.db", tmp_path / "restored.db")

    with SessionStore(db_path) as store, pytest.raises(ValueError, match="destination"):
        store.backup(db_path)


def test_restore_requires_expected_schema(tmp_path: Path) -> None:
    backup_path = tmp_path / "wrong.db"
    db_path = tmp_path / "restored.db"
    conn = sqlite3.connect(backup_path)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="missing tables"):
        SessionStore.restore_backup(backup_path, db_path)


def test_legacy_session_database_registers_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            workspace TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    with SessionStore(db_path) as store:
        assert store.schema_version() == SCHEMA_VERSION


def test_future_session_schema_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    conn.commit()
    conn.close()

    store = SessionStore(db_path)
    with pytest.raises(ValueError, match="newer than supported"):
        store.open()
    store.close()


@pytest.mark.asyncio
async def test_session_backup_cli_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    db_path = workspace / ".carbide" / "sessions.db"
    _seed_store(db_path)
    backup_path = tmp_path / "sessions-backup.db"
    console = Console(record=True)

    assert await check_sessions(workspace, console) == 0
    assert await backup_sessions(workspace, backup_path, False, console) == 0
    assert await migrate_sessions(workspace, console) == 0

    restored_workspace = tmp_path / "restored-workspace"
    assert await restore_sessions(restored_workspace, backup_path, False, console) == 0
    assert await check_sessions(restored_workspace, console) == 0


@pytest.mark.asyncio
async def test_session_cli_reports_future_schema_errors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    db_path = workspace / ".carbide" / "sessions.db"
    _seed_store(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    connection.commit()
    connection.close()
    console = Console(record=True)

    assert await check_sessions(workspace, console) == 1
    assert await backup_sessions(workspace, tmp_path / "backup.db", False, console) == 1
