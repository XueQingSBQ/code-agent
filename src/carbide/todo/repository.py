"""SQL repository for session-scoped Todo data.

The repository owns SQL mapping only. Business rules remain in ``TodoService``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from carbide.governance.session_store import SessionStore
from carbide.todo.domain import TodoCheckpoint, TodoItem, TodoSnapshot, TodoStatus


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TodoRepository:
    """Persistence adapter backed by the active ``SessionStore`` connection."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._store.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._store.transaction() as conn:
            yield conn

    def load_snapshot(self, conn: sqlite3.Connection, session_id: str) -> TodoSnapshot | None:
        plan = conn.execute(
            "SELECT plan_version, created_at, updated_at FROM todo_plans WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if plan is None:
            return None

        rows = conn.execute(
            "SELECT id, title, description, status, blocked_reason, owner_id, lease_until, "
            "position, revision, created_at, updated_at FROM todo_items "
            "WHERE session_id = ? ORDER BY position, id",
            (session_id,),
        ).fetchall()
        dependencies: dict[str, list[str]] = {str(row[0]): [] for row in rows}
        for item_id, depends_on_id in conn.execute(
            "SELECT item_id, depends_on_id FROM todo_dependencies "
            "WHERE session_id = ? ORDER BY item_id, depends_on_id",
            (session_id,),
        ):
            dependencies[str(item_id)].append(str(depends_on_id))

        items = tuple(
            TodoItem(
                id=str(row[0]),
                title=str(row[1]),
                description=str(row[2]),
                status=TodoStatus(str(row[3])),
                blocked_reason=str(row[4]) if row[4] is not None else None,
                owner_id=str(row[5]) if row[5] is not None else None,
                lease_until=str(row[6]) if row[6] is not None else None,
                position=int(row[7]),
                revision=int(row[8]),
                created_at=str(row[9]),
                updated_at=str(row[10]),
                dependencies=tuple(dependencies[str(row[0])]),
            )
            for row in rows
        )
        return TodoSnapshot(
            session_id=session_id,
            plan_version=int(plan[0]),
            items=items,
            created_at=str(plan[1]),
            updated_at=str(plan[2]),
        )

    def create_plan(self, conn: sqlite3.Connection, session_id: str) -> None:
        now = _now()
        conn.execute(
            "INSERT INTO todo_plans (session_id, plan_version, created_at, updated_at) "
            "VALUES (?, 0, ?, ?)",
            (session_id, now, now),
        )

    def add_item(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        item: TodoItem,
    ) -> None:
        conn.execute(
            "INSERT INTO todo_items "
            "(id, session_id, title, description, status, blocked_reason, owner_id, lease_until, "
            "position, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                session_id,
                item.title,
                item.description,
                item.status.value,
                item.blocked_reason,
                item.owner_id,
                item.lease_until,
                item.position,
                item.revision,
                item.created_at,
                item.updated_at,
            ),
        )
        self._replace_dependencies(conn, session_id, item.id, item.dependencies)

    def update_item(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        item: TodoItem,
    ) -> None:
        conn.execute(
            "UPDATE todo_items SET title = ?, description = ?, status = ?, "
            "blocked_reason = ?, owner_id = ?, lease_until = ?, revision = ?, updated_at = ? "
            "WHERE id = ? AND session_id = ?",
            (
                item.title,
                item.description,
                item.status.value,
                item.blocked_reason,
                item.owner_id,
                item.lease_until,
                item.revision,
                item.updated_at,
                item.id,
                session_id,
            ),
        )
        self._replace_dependencies(conn, session_id, item.id, item.dependencies)

    def remove_item(self, conn: sqlite3.Connection, session_id: str, item_id: str) -> None:
        conn.execute(
            "DELETE FROM todo_items WHERE id = ? AND session_id = ?", (item_id, session_id)
        )

    def replace_items(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        items: tuple[TodoItem, ...],
    ) -> None:
        """Replace all items atomically; dependencies are inserted second."""
        conn.execute("DELETE FROM todo_dependencies WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM todo_items WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO todo_items "
            "(id, session_id, title, description, status, blocked_reason, owner_id, lease_until, "
            "position, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.id,
                    session_id,
                    item.title,
                    item.description,
                    item.status.value,
                    item.blocked_reason,
                    item.owner_id,
                    item.lease_until,
                    item.position,
                    item.revision,
                    item.created_at,
                    item.updated_at,
                )
                for item in items
            ],
        )
        conn.executemany(
            "INSERT INTO todo_dependencies (session_id, item_id, depends_on_id) VALUES (?, ?, ?)",
            [
                (session_id, item.id, dependency_id)
                for item in items
                for dependency_id in item.dependencies
            ],
        )

    def create_checkpoint(
        self,
        conn: sqlite3.Connection,
        *,
        checkpoint_id: str,
        session_id: str,
        label: str,
        source_plan_version: int,
        snapshot: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO todo_checkpoints "
            "(id, session_id, label, source_plan_version, snapshot, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                checkpoint_id,
                session_id,
                label,
                source_plan_version,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def list_checkpoints(
        self, conn: sqlite3.Connection, session_id: str, limit: int
    ) -> tuple[TodoCheckpoint, ...]:
        rows = conn.execute(
            "SELECT id, label, source_plan_version, created_at FROM todo_checkpoints "
            "WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return tuple(
            TodoCheckpoint(
                id=str(row[0]),
                label=str(row[1]),
                source_plan_version=int(row[2]),
                created_at=str(row[3]),
            )
            for row in rows
        )

    def load_checkpoint(
        self, conn: sqlite3.Connection, session_id: str, checkpoint_id: str
    ) -> tuple[TodoCheckpoint, dict[str, Any]] | None:
        row = conn.execute(
            "SELECT id, label, source_plan_version, snapshot, created_at "
            "FROM todo_checkpoints WHERE id = ? AND session_id = ?",
            (checkpoint_id, session_id),
        ).fetchone()
        if row is None:
            return None
        return (
            TodoCheckpoint(
                id=str(row[0]),
                label=str(row[1]),
                source_plan_version=int(row[2]),
                created_at=str(row[4]),
            ),
            json.loads(str(row[3])),
        )

    def bump_plan_version(self, conn: sqlite3.Connection, session_id: str, version: int) -> None:
        conn.execute(
            "UPDATE todo_plans SET plan_version = ?, updated_at = ? WHERE session_id = ?",
            (version, _now(), session_id),
        )

    def append_event(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        plan_version: int,
        actor_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO todo_events "
            "(id, session_id, plan_version, actor_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                plan_version,
                actor_id,
                kind,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def _replace_dependencies(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        item_id: str,
        dependencies: tuple[str, ...],
    ) -> None:
        conn.execute(
            "DELETE FROM todo_dependencies WHERE session_id = ? AND item_id = ?",
            (session_id, item_id),
        )
        conn.executemany(
            "INSERT INTO todo_dependencies (session_id, item_id, depends_on_id) VALUES (?, ?, ?)",
            [(session_id, item_id, dependency_id) for dependency_id in dependencies],
        )
