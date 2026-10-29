"""Tests for SQLite session storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from carbide.governance import SessionStore


def test_session_store_creates_database(tmp_path: Path) -> None:
    """SessionStore creates the database file and initializes schema."""
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)

    store.open()
    store.close()

    assert db_path.exists()


def test_create_and_load_session(tmp_path: Path) -> None:
    """Sessions can be created and retrieved."""
    db_path = tmp_path / "sessions.db"
    workspace = Path("/workspace")

    with SessionStore(db_path) as store:
        store.create_session("session-1", workspace)
        info = store.get_session_info("session-1")

    assert info is not None
    assert info["session_id"] == "session-1"
    assert info["workspace"] == str(workspace)
    assert info["status"] == "running"


def test_append_and_load_messages(tmp_path: Path) -> None:
    """Messages can be appended and retrieved in order."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))
        store.append_message("session-1", "user", [{"type": "text", "text": "hello"}])
        store.append_message("session-1", "assistant", [{"type": "text", "text": "hi"}])

        messages = store.load_messages("session-1")

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"][0]["text"] == "hello"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0]["text"] == "hi"


def test_update_session_status(tmp_path: Path) -> None:
    """Session status can be updated."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))
        store.update_session_status("session-1", "completed")

        info = store.get_session_info("session-1")

    assert info is not None
    assert info["status"] == "completed"


def test_session_status_contract_is_validated(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))
        with pytest.raises(ValueError, match="Invalid session status"):
            store.update_session_status("session-1", "unknown")
        with pytest.raises(ValueError, match="Invalid session status"):
            store.list_sessions(status="unknown")
        with pytest.raises(ValueError, match="limit must be positive"):
            store.list_sessions(limit=0)


def test_session_exists(tmp_path: Path) -> None:
    """session_exists correctly identifies existing sessions."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))

        assert store.session_exists("session-1")
        assert not store.session_exists("session-999")


def test_list_sessions(tmp_path: Path) -> None:
    """list_sessions returns recent sessions ordered by update time."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace1"))
        store.create_session("session-2", Path("/workspace2"))
        store.create_session("session-3", Path("/workspace3"))

        # Update session-1 to make it most recent
        store.append_message("session-1", "user", [{"type": "text", "text": "test"}])

        sessions = store.list_sessions(limit=10)

    assert len(sessions) == 3
    # Most recently updated first
    assert sessions[0]["session_id"] == "session-1"
    assert sessions[1]["session_id"] == "session-3"
    assert sessions[2]["session_id"] == "session-2"


def test_list_sessions_includes_summary_pagination_and_search(tmp_path: Path) -> None:
    """Session discovery must not depend on remembering opaque UUIDs."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))
        store.append_message(
            "session-1",
            "user",
            [{"type": "text", "text": "Implement the database migration"}],
        )
        store.append_message(
            "session-1", "assistant", [{"type": "text", "text": "Migration completed"}]
        )
        store.create_session("session-2", Path("/workspace"))
        store.append_message(
            "session-2", "user", [{"type": "text", "text": "Fix the login page"}]
        )

        first_page = store.list_sessions(limit=1)
        second_page = store.list_sessions(limit=1, offset=1)
        searched = store.list_sessions(search="database")

    assert len(first_page) == 1
    assert first_page[0]["title"] == "Fix the login page"
    assert first_page[0]["message_count"] == 1
    assert second_page[0]["title"] == "Implement the database migration"
    assert second_page[0]["preview"] == "Migration completed"
    assert [session["session_id"] for session in searched] == ["session-1"]


def test_list_sessions_rejects_negative_offset(tmp_path: Path) -> None:
    with (
        SessionStore(tmp_path / "sessions.db") as store,
        pytest.raises(ValueError, match="offset must be non-negative"),
    ):
        store.list_sessions(offset=-1)


def test_resume_preserves_message_order(tmp_path: Path) -> None:
    """Messages maintain chronological order across sessions."""
    db_path = tmp_path / "sessions.db"

    # First session: add 3 messages
    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace"))
        store.append_message("session-1", "user", [{"type": "text", "text": "first"}])
        store.append_message("session-1", "assistant", [{"type": "text", "text": "second"}])
        store.append_message("session-1", "user", [{"type": "text", "text": "third"}])

    # Resume session: load messages
    with SessionStore(db_path) as store:
        messages = store.load_messages("session-1")

    assert len(messages) == 3
    assert messages[0]["content"][0]["text"] == "first"
    assert messages[1]["content"][0]["text"] == "second"
    assert messages[2]["content"][0]["text"] == "third"


def test_concurrent_sessions_isolated(tmp_path: Path) -> None:
    """Messages from different sessions don't interfere."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        store.create_session("session-1", Path("/workspace1"))
        store.create_session("session-2", Path("/workspace2"))

        store.append_message("session-1", "user", [{"type": "text", "text": "msg-1"}])
        store.append_message("session-2", "user", [{"type": "text", "text": "msg-2"}])

        messages_1 = store.load_messages("session-1")
        messages_2 = store.load_messages("session-2")

    assert len(messages_1) == 1
    assert len(messages_2) == 1
    assert messages_1[0]["content"][0]["text"] == "msg-1"
    assert messages_2[0]["content"][0]["text"] == "msg-2"


def test_get_nonexistent_session_returns_none(tmp_path: Path) -> None:
    """get_session_info returns None for missing sessions."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        info = store.get_session_info("nonexistent")

    assert info is None


def test_load_messages_empty_for_nonexistent_session(tmp_path: Path) -> None:
    """load_messages returns empty list for nonexistent sessions."""
    db_path = tmp_path / "sessions.db"

    with SessionStore(db_path) as store:
        messages = store.load_messages("nonexistent")

    assert messages == []


def test_session_store_raises_if_not_opened(tmp_path: Path) -> None:
    """Operations without opening raise RuntimeError."""
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)

    with pytest.raises(RuntimeError, match="not opened"):
        store.create_session("session-1", Path("/workspace"))
