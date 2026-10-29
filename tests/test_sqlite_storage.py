"""Test SQLite storage functionality."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from carbide.storage.sqlite import SessionStorage


def test_session_crud():
    """Test session create/read/update/delete."""
    print("\n[TEST] Session CRUD operations...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = SessionStorage(db_path)

        try:
            # Create session
            storage.create_session(
                session_id="test-123",
                workspace="/tmp/test",
                model="gpt-5.6-luna",
                metadata={"user": "test_user"},
            )

            # Read session
            session = storage.get_session("test-123")
            assert session is not None
            assert session["session_id"] == "test-123"
            assert session["workspace"] == "/tmp/test"
            assert session["model"] == "gpt-5.6-luna"
            print("  [PASS] Create and read session")

            # Update session
            storage.update_session("test-123", metadata={"status": "active"})
            updated = storage.get_session("test-123")
            assert "status" in updated["metadata"]
            print("  [PASS] Update session")

            # Delete session
            storage.delete_session("test-123")
            deleted = storage.get_session("test-123")
            assert deleted is None
            print("  [PASS] Delete session")

        finally:
            storage.close()

    print("[PASS] Session CRUD operations")


def test_message_history():
    """Test message storage and retrieval."""
    print("\n[TEST] Message history...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = SessionStorage(db_path)

        try:
            storage.create_session("test-123", "/tmp/test", "gpt-5.6-luna")

            # Add messages
            msg1_id = storage.add_message("test-123", "user", "Hello", token_count=5)
            msg2_id = storage.add_message("test-123", "assistant", "Hi there", token_count=10)
            msg3_id = storage.add_message("test-123", "user", "How are you?", token_count=8)

            assert msg1_id > 0
            assert msg2_id > msg1_id
            assert msg3_id > msg2_id
            print("  [PASS] Add messages")

            # Get all messages
            messages = storage.get_messages("test-123")
            assert len(messages) == 3
            assert messages[0]["content"] == "Hello"
            assert messages[1]["content"] == "Hi there"
            assert messages[2]["content"] == "How are you?"
            print("  [PASS] Retrieve all messages")

            # Get messages with limit
            limited = storage.get_messages("test-123", limit=2)
            assert len(limited) == 2
            print("  [PASS] Retrieve with limit")

            # Get messages after ID
            after = storage.get_messages("test-123", after_id=msg1_id)
            assert len(after) == 2
            assert after[0]["id"] == msg2_id
            print("  [PASS] Retrieve after ID")

        finally:
            storage.close()

    print("[PASS] Message history")


def test_tool_execution_tracking():
    """Test tool execution recording."""
    print("\n[TEST] Tool execution tracking...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = SessionStorage(db_path)

        try:
            storage.create_session("test-123", "/tmp/test", "gpt-5.6-luna")

            # Record successful tool execution
            exec1_id = storage.add_tool_execution(
                session_id="test-123",
                tool_name="bash",
                arguments={"command": "ls -la"},
                result="total 0\ndrwxr-xr-x",
                is_error=False,
                duration_ms=123.45,
            )
            assert exec1_id > 0
            print("  [PASS] Record successful execution")

            # Record failed tool execution
            exec2_id = storage.add_tool_execution(
                session_id="test-123",
                tool_name="bash",
                arguments={"command": "false"},
                result="exit 1",
                is_error=True,
                duration_ms=50.0,
            )
            assert exec2_id > exec1_id
            print("  [PASS] Record failed execution")

            # Retrieve executions
            executions = storage.get_tool_executions("test-123")
            assert len(executions) == 2
            # Most recent first
            assert executions[0]["tool_name"] == "bash"
            assert executions[0]["is_error"] == 1
            assert executions[1]["is_error"] == 0
            print("  [PASS] Retrieve executions")

            # Retrieve with limit
            limited = storage.get_tool_executions("test-123", limit=1)
            assert len(limited) == 1
            print("  [PASS] Retrieve with limit")

        finally:
            storage.close()

    print("[PASS] Tool execution tracking")


def test_context_snapshots():
    """Test context compression snapshot tracking."""
    print("\n[TEST] Context snapshots...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = SessionStorage(db_path)

        try:
            storage.create_session("test-123", "/tmp/test", "gpt-5.6-luna")

            # Record snapshots at different levels
            snap1_id = storage.add_context_snapshot(
                session_id="test-123",
                level="L1",
                token_count=50000,
                snapshot_data={"trigger": "spill"},
            )

            snap2_id = storage.add_context_snapshot(
                session_id="test-123",
                level="L2",
                token_count=75000,
                snapshot_data={"trigger": "snip"},
            )

            assert snap1_id > 0
            assert snap2_id > snap1_id
            print("  [PASS] Record snapshots")

        finally:
            storage.close()

    print("[PASS] Context snapshots")


def test_list_sessions():
    """Test listing multiple sessions."""
    print("\n[TEST] List sessions...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        storage = SessionStorage(db_path)

        try:
            # Create multiple sessions
            storage.create_session("session-1", "/tmp/test1", "gpt-5.6-luna")
            storage.create_session("session-2", "/tmp/test2", "gpt-5.6-luna")
            storage.create_session("session-3", "/tmp/test3", "claude-sonnet-5")

            # List all
            sessions = storage.list_sessions()
            assert len(sessions) == 3
            # Most recent first
            assert sessions[0]["session_id"] == "session-3"
            print("  [PASS] List all sessions")

            # List with limit
            limited = storage.list_sessions(limit=2)
            assert len(limited) == 2
            print("  [PASS] List with limit")

        finally:
            storage.close()

    print("[PASS] List sessions")


def test_database_persistence():
    """Test that data persists across connections."""
    print("\n[TEST] Database persistence...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"

        # First connection
        storage1 = SessionStorage(db_path)
        storage1.create_session("persist-123", "/tmp/test", "gpt-5.6-luna")
        storage1.add_message("persist-123", "user", "Test message")
        storage1.close()

        # Second connection
        storage2 = SessionStorage(db_path)
        session = storage2.get_session("persist-123")
        messages = storage2.get_messages("persist-123")

        assert session is not None
        assert len(messages) == 1
        assert messages[0]["content"] == "Test message"
        storage2.close()

        print("  [PASS] Data persists across connections")

    print("[PASS] Database persistence")


def main():
    print("=" * 60)
    print("Testing SQLite Storage (M2 Core Deliverable)")
    print("=" * 60)

    tests = [
        test_session_crud,
        test_message_history,
        test_tool_execution_tracking,
        test_context_snapshots,
        test_list_sessions,
        test_database_persistence,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
