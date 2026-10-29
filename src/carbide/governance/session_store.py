"""SQLite session storage for persisting conversation history.

M2 governance layer: durable session storage that allows resuming conversations
across process restarts. Stores sessions and their message history in SQLite
with WAL mode for better concurrency.

Schema:
- sessions: session metadata (id, workspace, status, timestamps)
- messages: conversation messages (role, content JSON, timestamps)
- todo_*: one session-scoped plan, tasks, dependencies, events, and checkpoints
- agent_runs/agent_messages: durable parent-child Agent execution state
- schema_meta: schema version metadata for controlled upgrades
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 7

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""

TODO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todo_plans (
    session_id TEXT PRIMARY KEY,
    plan_version INTEGER NOT NULL DEFAULT 0 CHECK (plan_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS todo_items (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    blocked_reason TEXT,
    owner_id TEXT,
    lease_until TEXT,
    position INTEGER NOT NULL CHECK (position >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES todo_plans(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_todo_items_session_position
    ON todo_items(session_id, position, id);

CREATE TABLE IF NOT EXISTS todo_dependencies (
    session_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    depends_on_id TEXT NOT NULL,
    PRIMARY KEY (session_id, item_id, depends_on_id),
    CHECK (item_id <> depends_on_id),
    FOREIGN KEY (item_id) REFERENCES todo_items(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on_id) REFERENCES todo_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_todo_dependencies_target
    ON todo_dependencies(session_id, depends_on_id);

CREATE TABLE IF NOT EXISTS todo_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES todo_plans(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_todo_events_session
    ON todo_events(session_id, id);

CREATE TABLE IF NOT EXISTS todo_checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    label TEXT NOT NULL,
    source_plan_version INTEGER NOT NULL CHECK (source_plan_version >= 0),
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES todo_plans(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_todo_checkpoints_session
    ON todo_checkpoints(session_id, created_at, id);
"""

AGENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    child_session_id TEXT,
    mode TEXT NOT NULL,
    role TEXT,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    background INTEGER NOT NULL DEFAULT 0 CHECK (background IN (0, 1)),
    result TEXT,
    summary TEXT,
    error TEXT,
    usage TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner_id TEXT,
    lease_until TEXT,
    completed_at TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (child_session_id) REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_parent
    ON agent_runs(parent_session_id, created_at, id);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    acknowledged_at TEXT,
    delivery_owner TEXT,
    delivery_token TEXT,
    delivery_until TEXT,
    FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_inbox
    ON agent_messages(run_id, direction, status, id);
"""


def _message_text(content: Any) -> str:
    """Extract human-readable text from a persisted message payload."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_message_text(item) for item in content]
        return " ".join(part for part in parts if part)
    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type == "tool_use":
            return f"[tool: {content.get('name', 'unknown')}]"
        if isinstance(content.get("text"), str):
            return cast(str, content["text"])
        if "content" in content:
            return _message_text(content["content"])
    return ""


def _decode_message_preview(raw_content: str | None, *, limit: int) -> str:
    """Decode and bound a message preview without letting corrupt JSON break listing."""

    if not raw_content:
        return ""
    try:
        text = _message_text(json.loads(raw_content))
    except (TypeError, json.JSONDecodeError):
        text = raw_content
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _escape_like(value: str) -> str:
    """Escape a user search term for a literal SQLite LIKE query."""

    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


VALID_SESSION_STATUSES = frozenset({"running", "completed", "failed"})
VALID_AGENT_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_for_parent",
        "completed",
        "failed",
        "cancelled",
        "merge_blocked",
        "orphaned",
    }
)
VALID_AGENT_MESSAGE_DIRECTIONS = frozenset({"parent_to_child", "child_to_parent"})
VALID_AGENT_MESSAGE_STATUSES = frozenset({"queued", "delivered", "acknowledged"})
ACTIVE_AGENT_STATUSES = frozenset({"queued", "running", "waiting_for_parent"})
TERMINAL_AGENT_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "merge_blocked", "orphaned"}
)


def _lease_deadline(seconds: float | None) -> str | None:
    """Return an absolute UTC lease deadline for a positive duration."""
    if seconds is None:
        return None
    if seconds <= 0:
        raise ValueError("lease duration must be positive")
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class SessionStore:
    """SQLite-backed session storage."""

    def __init__(self, db_path: Path, *, ephemeral: bool = False) -> None:
        self._db_path = db_path
        self._ephemeral = ephemeral
        self._conn: sqlite3.Connection | None = None
        self._opened = False
        # A logical consumer survives ephemeral connection recycling, but not
        # an explicit close. Tokens fence deliveries reclaimed by another store.
        self._delivery_owner = str(uuid.uuid4())
        self._has_deliveries = False

    def open(self) -> None:
        """Open the database connection and initialize schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._opened = True
        self._open_connection()
        try:
            self._init_schema()
        except Exception:
            self.close()
            raise
        finally:
            self._release_connection()

    def _open_connection(self) -> sqlite3.Connection:
        if not self._opened:
            raise RuntimeError("SessionStore not opened")
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging
        return self._conn

    def _release_connection(self) -> None:
        if self._ephemeral and self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Borrow a connection and release it for short-lived Agent stores."""
        conn = self._open_connection()
        try:
            yield conn
        finally:
            self._release_connection()

    def _init_schema(self) -> None:
        """Create tables and migrate a compatible database to the current schema."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("SessionStore not opened")
        conn.executescript(SCHEMA_SQL)
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            # Databases created before schema metadata was introduced already
            # had the v1 tables. Register that legacy layout as v1.
            conn.executescript(TODO_SCHEMA_SQL)
            conn.executescript(AGENT_SCHEMA_SQL)
            self._ensure_agent_lease_columns(conn)
            self._ensure_message_delivery_columns(conn)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid session database schema version") from exc
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"Session database schema v{version} is newer than supported v{SCHEMA_VERSION}"
                )
            if version < SCHEMA_VERSION:
                if version < 3:
                    conn.executescript(TODO_SCHEMA_SQL)
                if version < 4:
                    self._ensure_todo_ownership_columns(conn)
                if version < 5:
                    conn.executescript(AGENT_SCHEMA_SQL)
                if version < 6:
                    self._ensure_agent_lease_columns(conn)
                if version < 7:
                    self._ensure_message_delivery_columns(conn)
                conn.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            else:
                # Idempotent repair for databases where the schema version was
                # written immediately before a process interruption.
                conn.executescript(TODO_SCHEMA_SQL)
                conn.executescript(AGENT_SCHEMA_SQL)
                self._ensure_agent_lease_columns(conn)
                self._ensure_message_delivery_columns(conn)
        conn.commit()

    @staticmethod
    def _ensure_todo_ownership_columns(conn: sqlite3.Connection) -> None:
        """Add future multi-agent columns to pre-v4 Todo databases."""
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(todo_items)").fetchall()}
        if "owner_id" not in columns:
            conn.execute("ALTER TABLE todo_items ADD COLUMN owner_id TEXT")
        if "lease_until" not in columns:
            conn.execute("ALTER TABLE todo_items ADD COLUMN lease_until TEXT")

    @staticmethod
    def _ensure_agent_lease_columns(conn: sqlite3.Connection) -> None:
        """Add process-ownership columns to pre-v6 Agent databases."""
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
        if "owner_id" not in columns:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN owner_id TEXT")
        if "lease_until" not in columns:
            conn.execute("ALTER TABLE agent_runs ADD COLUMN lease_until TEXT")

    @staticmethod
    def _ensure_message_delivery_columns(conn: sqlite3.Connection) -> None:
        """Make legacy unacknowledged deliveries eligible for lease recovery."""
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_messages)")}
        for name in ("delivery_owner", "delivery_token", "delivery_until"):
            if name not in columns:
                conn.execute(f"ALTER TABLE agent_messages ADD COLUMN {name} TEXT")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Borrow a connection for a read-only or caller-managed operation."""
        with self._connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a group of storage operations as one atomic transaction."""
        with self._connection() as conn:
            try:
                # Acquire the writer lock before the first read. Todo's
                # optimistic version check must observe a serialized writer
                # view; a deferred transaction could otherwise let two
                # writers read the same version and fail later with a raw
                # ``database is locked`` error.
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def schema_version(self) -> int:
        """Return the on-disk schema version after applying safe migrations."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise ValueError("Session database has no schema version")
            try:
                return int(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid session database schema version") from exc

    def create_session(self, session_id: str, workspace: Path) -> None:
        """Create a new session entry."""
        with self._connection() as conn:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at, workspace, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, now, now, str(workspace), "running"),
            )
            conn.commit()

    def append_message(
        self,
        session_id: str,
        role: str,
        content: list[dict[str, Any]],
    ) -> None:
        """Append a message to the session history."""
        with self.transaction() as conn:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, json.dumps(content), now),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))

    def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Atomically replace a session's messages.

        This is used when resuming a session requires protocol repair (for
        example, synthesising a missing ``tool_result``).  Persisting the
        repaired snapshot prevents the same repair from being rediscovered on
        every subsequent process restart.
        """
        with self._connection() as conn:
            now = datetime.now(UTC).isoformat()
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        session_id,
                        str(message.get("role", "user")),
                        json.dumps(message.get("content", [])),
                        now,
                    )
                    for message in messages
                ],
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            conn.commit()

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Load all messages for a session in chronological order."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            )
            messages = []
            for row in cursor:
                role, content_json = row
                messages.append({"role": role, "content": json.loads(content_json)})
            return messages

    def update_session_status(self, session_id: str, status: str) -> None:
        """Update session status (running, completed, failed)."""
        if status not in VALID_SESSION_STATUSES:
            allowed = ", ".join(sorted(VALID_SESSION_STATUSES))
            raise ValueError(f"Invalid session status {status!r}; expected one of: {allowed}")
        with self._connection() as conn:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
            conn.commit()

    def create_agent_run(
        self,
        *,
        run_id: str,
        parent_session_id: str,
        child_session_id: str | None,
        mode: str,
        role: str | None,
        task: str,
        background: bool,
        owner_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        """Create one durable parent-child Agent execution record."""
        now = datetime.now(UTC).isoformat()
        lease_until = _lease_deadline(lease_seconds) if owner_id is not None else None
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, parent_session_id, child_session_id, mode, role, task,
                    status, background, created_at, updated_at, owner_id, lease_until
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    parent_session_id,
                    child_session_id,
                    mode,
                    role,
                    task,
                    int(background),
                    now,
                    now,
                    owner_id,
                    lease_until,
                ),
            )
            conn.commit()

    def update_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        child_session_id: str | None = None,
        result: Any = None,
        summary: str | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        owner_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        """Update execution state and atomically persist its final payload."""
        if status not in VALID_AGENT_STATUSES:
            allowed = ", ".join(sorted(VALID_AGENT_STATUSES))
            raise ValueError(f"Invalid Agent status {status!r}; expected one of: {allowed}")

        now = datetime.now(UTC).isoformat()
        completed_at = (
            now
            if status
            in {
                "completed",
                "failed",
                "cancelled",
                "merge_blocked",
                "orphaned",
            }
            else None
        )
        lease_until = (
            _lease_deadline(lease_seconds)
            if owner_id is not None and status not in TERMINAL_AGENT_STATUSES
            else None
        )
        with self._connection() as conn:
            assignments = [
                "status = ?",
                "child_session_id = COALESCE(?, child_session_id)",
                "result = ?",
                "summary = ?",
                "error = ?",
                "usage = ?",
                "updated_at = ?",
                "completed_at = COALESCE(?, completed_at)",
            ]
            parameters: list[Any] = [
                status,
                child_session_id,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                summary,
                error,
                json.dumps(usage, ensure_ascii=False) if usage is not None else None,
                now,
                completed_at,
            ]
            if owner_id is not None:
                assignments.extend(["owner_id = ?", "lease_until = ?"])
                parameters.extend([owner_id, lease_until])
                where = "WHERE id = ? AND owner_id = ? AND status != 'orphaned'"
                parameters.extend([run_id, owner_id])
            else:
                assignments.append("lease_until = NULL")
                where = "WHERE id = ?"
                parameters.append(run_id)
            cursor = conn.execute(
                f"UPDATE agent_runs SET {', '.join(assignments)} {where}", parameters
            )
            conn.commit()
            return cursor.rowcount == 1

    def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        """Load one Agent execution record with decoded JSON payloads."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, parent_session_id, child_session_id, mode, role, task,
                       status, background, result, summary, error, usage,
                       created_at, updated_at, owner_id, lease_until, completed_at
                FROM agent_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_agent_run_row(row)

    def list_agent_runs(
        self,
        parent_session_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List child Agent runs owned by one parent session."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if status is not None and status not in VALID_AGENT_STATUSES:
            allowed = ", ".join(sorted(VALID_AGENT_STATUSES))
            raise ValueError(f"Invalid Agent status {status!r}; expected one of: {allowed}")

        query = """
            SELECT id, parent_session_id, child_session_id, mode, role, task,
                   status, background, result, summary, error, usage,
                   created_at, updated_at, owner_id, lease_until, completed_at
            FROM agent_runs
            WHERE parent_session_id = ?
        """
        parameters: list[Any] = [parent_session_id]
        if status is not None:
            query += " AND status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        parameters.append(limit)

        with self._connection() as conn:
            rows = conn.execute(query, parameters).fetchall()
        return [self._decode_agent_run_row(row) for row in rows]

    @staticmethod
    def _decode_agent_run_row(row: tuple[Any, ...]) -> dict[str, Any]:
        """Decode the stable row shape used by Agent status APIs."""
        result: Any = None
        usage: Any = None
        if row[8] is not None:
            with suppress(TypeError, json.JSONDecodeError):
                result = json.loads(row[8])
        if row[11] is not None:
            with suppress(TypeError, json.JSONDecodeError):
                usage = json.loads(row[11])
        return {
            "agent_id": row[0],
            "parent_session_id": row[1],
            "child_session_id": row[2],
            "mode": row[3],
            "role": row[4],
            "task": row[5],
            "status": row[6],
            "background": bool(row[7]),
            "result": result,
            "summary": row[9],
            "error": row[10],
            "usage": usage,
            "created_at": row[12],
            "updated_at": row[13],
            "owner_id": row[14],
            "lease_until": row[15],
            "completed_at": row[16],
        }

    def heartbeat_agent_runs(
        self,
        *,
        parent_session_id: str,
        owner_id: str,
        lease_seconds: float,
    ) -> int:
        """Renew all active child leases owned by one manager instance."""
        lease_until = _lease_deadline(lease_seconds)
        assert lease_until is not None
        now = datetime.now(UTC).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET lease_until = ?, updated_at = ?
                WHERE parent_session_id = ? AND owner_id = ?
                  AND status IN ('queued', 'running', 'waiting_for_parent')
                """,
                (lease_until, now, parent_session_id, owner_id),
            )
            conn.commit()
            return cursor.rowcount

    def reap_expired_agent_runs(
        self,
        *,
        parent_session_id: str,
        exclude_owner_id: str | None = None,
    ) -> list[str]:
        """Fence active child runs whose process lease has expired.

        A manager only reaps records for its parent session.  The optional
        owner exclusion makes the operation safe when two processes inspect
        the same session: an unexpired lease remains owned by its live
        manager, while an expired lease is converted to ``orphaned``.
        """
        now = datetime.now(UTC).isoformat()
        message = "SubAgent lease expired; its owner process is unavailable"
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM agent_runs
                WHERE parent_session_id = ?
                  AND status IN ('queued', 'running', 'waiting_for_parent')
                  AND (lease_until IS NULL OR lease_until < ?)
                  AND (? IS NULL OR owner_id IS NULL OR owner_id <> ?)
                """,
                (parent_session_id, now, exclude_owner_id, exclude_owner_id),
            ).fetchall()
            run_ids = [str(row[0]) for row in rows]
            if run_ids:
                placeholders = ", ".join("?" for _ in run_ids)
                conn.execute(
                    f"""
                    UPDATE agent_runs
                    SET status = 'orphaned',
                        error = COALESCE(error, ?),
                        owner_id = NULL,
                        lease_until = NULL,
                        updated_at = ?,
                        completed_at = COALESCE(completed_at, ?)
                    WHERE id IN ({placeholders})
                    """,
                    (message, now, now, *run_ids),
                )
        return run_ids

    def append_agent_message(
        self,
        *,
        run_id: str,
        direction: str,
        kind: str,
        content: Any,
        message_id: str | None = None,
        owner_id: str | None = None,
    ) -> str:
        """Append a durable parent-child message to a run inbox/outbox."""
        if direction not in VALID_AGENT_MESSAGE_DIRECTIONS:
            allowed = ", ".join(sorted(VALID_AGENT_MESSAGE_DIRECTIONS))
            raise ValueError(f"Invalid Agent message direction {direction!r}; expected {allowed}")
        identifier = message_id or str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self.transaction() as conn:
            payload = json.dumps(content, ensure_ascii=False)
            if owner_id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO agent_messages (
                        message_id, run_id, direction, kind, content, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (identifier, run_id, direction, kind, payload, now),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO agent_messages (
                        message_id, run_id, direction, kind, content, status, created_at
                    )
                    SELECT ?, ?, ?, ?, ?, 'queued', ?
                    WHERE EXISTS (
                        SELECT 1 FROM agent_runs
                        WHERE id = ? AND owner_id = ? AND status != 'orphaned'
                    )
                    """,
                    (
                        identifier,
                        run_id,
                        direction,
                        kind,
                        payload,
                        now,
                        run_id,
                        owner_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("SubAgent lease is no longer owned")
        return identifier

    def claim_agent_messages(
        self,
        *,
        run_id: str,
        direction: str,
        limit: int = 100,
        lease_seconds: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Lease queued or expired messages for one side of a child link."""
        return self._claim_agent_messages(
            run_id=run_id,
            parent_session_id=None,
            direction=direction,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    def claim_agent_messages_for_parent(
        self,
        *,
        parent_session_id: str,
        direction: str = "child_to_parent",
        limit: int = 100,
        lease_seconds: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Lease messages scoped to one parent's direct children."""
        return self._claim_agent_messages(
            run_id=None,
            parent_session_id=parent_session_id,
            direction=direction,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    def _claim_agent_messages(
        self,
        *,
        run_id: str | None,
        parent_session_id: str | None,
        direction: str,
        limit: int,
        lease_seconds: float,
    ) -> list[dict[str, Any]]:
        if direction not in VALID_AGENT_MESSAGE_DIRECTIONS:
            allowed = ", ".join(sorted(VALID_AGENT_MESSAGE_DIRECTIONS))
            raise ValueError(f"Invalid Agent message direction {direction!r}; expected {allowed}")
        if limit < 1:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease duration must be positive")
        claimed = []
        with self.transaction() as conn:
            now = datetime.now(UTC).isoformat()
            deadline = _lease_deadline(lease_seconds)
            rows = conn.execute(
                """
                SELECT messages.id, messages.message_id, messages.run_id,
                       messages.direction, messages.kind, messages.content,
                       messages.status, messages.created_at,
                       messages.delivered_at, messages.acknowledged_at,
                       messages.delivery_owner, messages.delivery_token, messages.delivery_until
                FROM agent_messages AS messages
                INNER JOIN agent_runs AS runs ON runs.id = messages.run_id
                WHERE (? IS NULL OR messages.run_id = ?)
                  AND (? IS NULL OR runs.parent_session_id = ?)
                  AND messages.direction = ?
                  AND (messages.status = 'queued' OR
                       (messages.status = 'delivered' AND
                        (messages.delivery_until IS NULL OR messages.delivery_until <= ?)))
                ORDER BY messages.id
                LIMIT ?
                """,
                (run_id, run_id, parent_session_id, parent_session_id, direction, now, limit),
            ).fetchall()
            for row in rows:
                token = str(uuid.uuid4())
                conn.execute(
                    """UPDATE agent_messages SET status = 'delivered', delivered_at = ?,
                       delivery_owner = ?, delivery_token = ?, delivery_until = ? WHERE id = ?""",
                    (now, self._delivery_owner, token, deadline, row[0]),
                )
                message = self._decode_agent_message_row(row)
                message.update(
                    status="delivered",
                    delivered_at=now,
                    delivery_owner=self._delivery_owner,
                    delivery_token=token,
                    delivery_until=deadline,
                )
                claimed.append(message)
        self._has_deliveries = self._has_deliveries or bool(claimed)
        return claimed

    def acknowledge_agent_message(self, message_id: str, *, delivery_token: str) -> bool:
        """ACK a current lease for non-context consumers.

        Agent runtime consumers must use accept_agent_message instead, so the
        context write and ACK cannot be separated by a crash.
        """
        with self.transaction() as conn:
            now = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                """
                UPDATE agent_messages
                SET status = 'acknowledged', acknowledged_at = ?
                WHERE message_id = ? AND status = 'delivered' AND delivery_token = ?
                  AND delivery_owner = ? AND delivery_until > ?
                """,
                (now, message_id, delivery_token, self._delivery_owner, now),
            )
            return cursor.rowcount == 1

    def accept_agent_message(
        self,
        *,
        session_id: str,
        message_id: str,
        delivery_token: str,
        content: list[dict[str, Any]],
    ) -> bool:
        """Atomically persist inbox context and ACK; return False for a duplicate.

        Delivery is at-least-once. Only durable context insertion is deduplicated;
        this does not make model calls or external tool effects exactly-once.
        """
        with self.transaction() as conn:
            now = datetime.now(UTC).isoformat()
            row = conn.execute(
                """SELECT m.status, m.delivery_owner, m.delivery_token, m.delivery_until,
                          CASE m.direction WHEN 'child_to_parent' THEN r.parent_session_id
                                           ELSE r.child_session_id END
                   FROM agent_messages m JOIN agent_runs r ON r.id = m.run_id
                   WHERE m.message_id = ?""",
                (message_id,),
            ).fetchone()
            if row is None or row[4] != session_id:
                raise ValueError("Agent message does not belong to the recipient session")
            if row[0] == "acknowledged":
                return False
            if (
                row[0] != "delivered"
                or row[1] != self._delivery_owner
                or row[2] != delivery_token
                or row[3] is None
                or row[3] <= now
            ):
                raise ValueError("Agent message delivery lease is no longer owned")
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, "user", json.dumps(content, ensure_ascii=False), now),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            conn.execute(
                """UPDATE agent_messages SET status = 'acknowledged', acknowledged_at = ?
                   WHERE message_id = ?""",
                (now, message_id),
            )
            return True

    @staticmethod
    def _decode_agent_message_row(
        row: tuple[Any, ...],
    ) -> dict[str, Any]:
        """Decode one message row without exposing raw JSON storage details."""
        try:
            content = json.loads(row[5])
        except (TypeError, json.JSONDecodeError):
            content = row[5]
        return {
            "id": row[1],
            "run_id": row[2],
            "direction": row[3],
            "kind": row[4],
            "content": content,
            "status": row[6],
            "created_at": row[7],
            "delivered_at": row[8],
            "acknowledged_at": row[9],
            "delivery_owner": row[10],
            "delivery_token": row[11],
            "delivery_until": row[12],
        }

    def session_exists(self, session_id: str) -> bool:
        """Check if a session exists."""
        with self._connection() as conn:
            cursor = conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,))
            return cursor.fetchone() is not None

    def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        """Get session metadata."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT created_at, updated_at, workspace, status FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            return {
                "session_id": session_id,
                "created_at": row[0],
                "updated_at": row[1],
                "workspace": row[2],
                "status": row[3],
            }

    def list_sessions(
        self,
        limit: int = 20,
        status: str | None = None,
        *,
        offset: int = 0,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent sessions, most recent first.

        Args:
            limit: Maximum number of sessions to return
            status: Filter by status (running/completed/failed), None for all
            offset: Number of matching sessions to skip
            search: Literal search across IDs, workspaces, and message content

        Returns:
            List of session dictionaries
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if status is not None and status not in VALID_SESSION_STATUSES:
            allowed = ", ".join(sorted(VALID_SESSION_STATUSES))
            raise ValueError(f"Invalid session status {status!r}; expected one of: {allowed}")

        where: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            where.append("s.status = ?")
            parameters.append(status)
        if search and search.strip():
            pattern = f"%{_escape_like(search.strip())}%"
            where.append(
                "(s.id LIKE ? ESCAPE '!' COLLATE NOCASE "
                "OR s.workspace LIKE ? ESCAPE '!' COLLATE NOCASE "
                "OR EXISTS ("
                "SELECT 1 FROM messages AS searched_message "
                "WHERE searched_message.session_id = s.id "
                "AND searched_message.content LIKE ? ESCAPE '!' COLLATE NOCASE"
                "))"
            )
            parameters.extend([pattern, pattern, pattern])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT
                    s.id,
                    s.created_at,
                    s.updated_at,
                    s.workspace,
                    s.status,
                    COUNT(message.id) AS message_count,
                    (
                        SELECT first_user.content
                        FROM messages AS first_user
                        WHERE first_user.session_id = s.id AND first_user.role = 'user'
                        ORDER BY first_user.id ASC
                        LIMIT 1
                    ) AS first_user_content,
                    (
                        SELECT last_message.content
                        FROM messages AS last_message
                        WHERE last_message.session_id = s.id
                        ORDER BY last_message.id DESC
                        LIMIT 1
                    ) AS last_message_content
                FROM sessions AS s
                LEFT JOIN messages AS message ON message.session_id = s.id
                {where_sql}
                GROUP BY s.id, s.created_at, s.updated_at, s.workspace, s.status
                ORDER BY s.updated_at DESC, s.id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            )

            sessions = []
            for row in cursor:
                sessions.append(
                    {
                        "id": row[0],
                        "session_id": row[0],
                        "created_at": row[1],
                        "updated_at": row[2],
                        "workspace": row[3],
                        "status": row[4],
                        "message_count": row[5],
                        "title": _decode_message_preview(row[6], limit=240) or "未命名会话",
                        "preview": _decode_message_preview(row[7], limit=600),
                    }
                )
            return sessions

    def count_messages(self, session_id: str) -> int:
        """Count the number of messages in a session.

        Args:
            session_id: Session ID to count messages for

        Returns:
            Number of messages in the session
        """
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            )
            result = cursor.fetchone()
            return result[0] if result else 0

    def integrity_check(self) -> str:
        """Run SQLite's consistency check and return its diagnostic result.

        A healthy database returns exactly ``"ok"``.  Database errors are
        returned as a diagnostic string so a CLI/health check can report the
        problem without losing the original SQLite message.
        """
        try:
            with self._connection() as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return str(row[0]) if row else "error: no integrity result"
        except sqlite3.DatabaseError as exc:
            return f"error: {exc}"

    def backup(self, destination: Path, *, overwrite: bool = False) -> Path:
        """Create a consistent online backup without holding the DB open.

        SQLite's online backup API is WAL-aware and can snapshot a live store.
        The temporary file is created next to the destination and atomically
        renamed after the backup finishes, so callers never observe a partial
        backup.  Existing backups are preserved unless ``overwrite=True``.
        """
        destination = Path(destination)
        source_path = self._db_path.resolve()
        destination_path = destination.resolve()
        if source_path == destination_path:
            raise ValueError("backup destination must differ from the session database")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Backup already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")

        try:
            with self._connection() as source:
                target = sqlite3.connect(str(temporary))
                try:
                    source.backup(target)
                finally:
                    target.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @classmethod
    def restore_backup(
        cls,
        backup_path: Path,
        db_path: Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Validate and atomically restore a backup into ``db_path``.

        The source is opened read-only by convention (no writes are issued),
        checked with ``PRAGMA integrity_check``, and required Carbide tables
        are verified before the destination is replaced.
        """
        backup_path = Path(backup_path)
        db_path = Path(db_path)
        if not backup_path.is_file():
            raise FileNotFoundError(f"Session backup not found: {backup_path}")
        if backup_path.resolve() == db_path.resolve():
            raise ValueError("restore source and destination must differ")
        if db_path.exists() and not overwrite:
            raise FileExistsError(f"Database already exists: {db_path}")

        source = sqlite3.connect(str(backup_path))
        temporary = db_path.with_name(f".{db_path.name}.{uuid.uuid4().hex}.restore")
        try:
            try:
                integrity = source.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise ValueError(f"Invalid session backup: {exc}") from exc
            if not integrity or integrity[0] != "ok":
                detail = integrity[0] if integrity else "no integrity result"
                raise ValueError(f"Invalid session backup: {detail}")
            tables = {
                row[0]
                for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = {"sessions", "messages"} - tables
            if missing:
                raise ValueError(
                    f"Invalid session backup; missing tables: {', '.join(sorted(missing))}"
                )

            db_path.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(str(temporary))
            try:
                source.backup(target)
            finally:
                target.close()
            temporary.replace(db_path)
        finally:
            source.close()
            temporary.unlink(missing_ok=True)
        return db_path

    def close(self) -> None:
        """Release unacknowledged deliveries on graceful consumer shutdown.

        An abrupt exit cannot run this cleanup; its deliveries become eligible
        when their leases expire. Ephemeral connection recycling is not shutdown.
        """
        try:
            if self._opened and self._has_deliveries:
                with self.transaction() as conn:
                    conn.execute(
                        """UPDATE agent_messages SET status = 'queued', delivery_owner = NULL,
                           delivery_token = NULL, delivery_until = NULL
                           WHERE status = 'delivered' AND delivery_owner = ?""",
                        (self._delivery_owner,),
                    )
        finally:
            if self._conn:
                self._conn.close()
                self._conn = None
            self._opened = False
            self._has_deliveries = False

    def __enter__(self) -> SessionStore:
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
