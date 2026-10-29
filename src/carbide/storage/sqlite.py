"""SQLite storage for session persistence (M2 core deliverable).

Stores conversation history, tool executions, and session state to enable
--resume functionality for long-running tasks.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SessionStorage:
    """SQLite-based storage for session state and history."""

    def __init__(self, db_path: str | Path):
        """Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection | None = None
        self._init_db()

    def _require_connection(self) -> sqlite3.Connection:
        """Return the open connection or fail with a useful lifecycle error."""
        if self.conn is None:
            raise RuntimeError("SessionStorage is closed")
        return self.conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        self.conn = conn

        cursor = conn.cursor()

        # Sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT
            )
        """)

        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                token_count INTEGER,
                metadata TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)

        # Tool executions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tool_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                result TEXT,
                is_error INTEGER NOT NULL,
                duration_ms REAL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)

        # Context snapshots table (for compression tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                level TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                snapshot_data TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)

        # Indexes for performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_executions(session_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_session ON context_snapshots(session_id)"
        )

        conn.commit()

    def create_session(
        self,
        session_id: str,
        workspace: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create a new session record.

        Args:
            session_id: Unique session identifier
            workspace: Workspace directory path
            model: Model name
            metadata: Optional session metadata
        """
        now = datetime.now(UTC).isoformat()
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO sessions (session_id, workspace, model, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, workspace, model, now, now, json.dumps(metadata or {})),
        )
        conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get session metadata.

        Args:
            session_id: Session identifier

        Returns:
            Session data dict or None if not found
        """
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def update_session(self, session_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Update session timestamp and metadata.

        Args:
            session_id: Session identifier
            metadata: Optional metadata to merge
        """
        now = datetime.now(UTC).isoformat()
        conn = self._require_connection()
        cursor = conn.cursor()

        if metadata:
            # Get existing metadata and merge
            existing = self.get_session(session_id)
            if existing:
                existing_meta = json.loads(existing.get("metadata", "{}"))
                existing_meta.update(metadata)
                metadata = existing_meta

        cursor.execute(
            "UPDATE sessions SET updated_at = ?, metadata = ? WHERE session_id = ?",
            (now, json.dumps(metadata or {}), session_id),
        )
        conn.commit()

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        token_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Add a message to the session history.

        Args:
            session_id: Session identifier
            role: Message role (user/assistant/system)
            content: Message content
            token_count: Optional token count
            metadata: Optional message metadata

        Returns:
            Message ID
        """
        now = datetime.now(UTC).isoformat()
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp, token_count, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, now, token_count, json.dumps(metadata or {})),
        )
        conn.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return the inserted message ID")
        return cursor.lastrowid

    def get_messages(
        self,
        session_id: str,
        limit: int | None = None,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get messages for a session.

        Args:
            session_id: Session identifier
            limit: Optional maximum number of messages
            after_id: Optional message ID to get messages after

        Returns:
            List of message dicts
        """
        conn = self._require_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM messages WHERE session_id = ?"
        params: list[Any] = [session_id]

        if after_id:
            query += " AND id > ?"
            params.append(after_id)

        query += " ORDER BY id ASC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def add_tool_execution(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: str | None = None,
        is_error: bool = False,
        duration_ms: float | None = None,
    ) -> int:
        """Record a tool execution.

        Args:
            session_id: Session identifier
            tool_name: Name of the tool
            arguments: Tool arguments
            result: Tool result content
            is_error: Whether execution failed
            duration_ms: Execution duration in milliseconds

        Returns:
            Tool execution ID
        """
        now = datetime.now(UTC).isoformat()
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tool_executions
            (session_id, tool_name, arguments, result, is_error, duration_ms, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                tool_name,
                json.dumps(arguments),
                result,
                1 if is_error else 0,
                duration_ms,
                now,
            ),
        )
        conn.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return the inserted tool execution ID")
        return cursor.lastrowid

    def get_tool_executions(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get tool execution history.

        Args:
            session_id: Session identifier
            limit: Optional maximum number of records

        Returns:
            List of tool execution dicts
        """
        conn = self._require_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM tool_executions WHERE session_id = ? ORDER BY id DESC"
        params: list[Any] = [session_id]

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def add_context_snapshot(
        self,
        session_id: str,
        level: str,
        token_count: int,
        snapshot_data: dict[str, Any] | None = None,
    ) -> int:
        """Record a context compression snapshot.

        Args:
            session_id: Session identifier
            level: Compression level (L1/L2/L3/L4)
            token_count: Token count at this snapshot
            snapshot_data: Optional snapshot metadata

        Returns:
            Snapshot ID
        """
        now = datetime.now(UTC).isoformat()
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO context_snapshots (session_id, level, token_count, timestamp, snapshot_data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, level, token_count, now, json.dumps(snapshot_data or {})),
        )
        conn.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return the inserted snapshot ID")
        return cursor.lastrowid

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent sessions.

        Args:
            limit: Maximum number of sessions to return

        Returns:
            List of session dicts
        """
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def delete_session(self, session_id: str) -> None:
        """Delete a session and all related data.

        Args:
            session_id: Session identifier
        """
        conn = self._require_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM tool_executions WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM context_snapshots WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> SessionStorage:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
