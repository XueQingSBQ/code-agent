"""Business rules for session-scoped Todo plans."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from carbide.todo.domain import TodoCheckpoint, TodoClaim, TodoItem, TodoSnapshot, TodoStatus
from carbide.todo.errors import (
    TodoConflictError,
    TodoDependencyError,
    TodoError,
    TodoPlanExistsError,
    TodoValidationError,
)
from carbide.todo.repository import TodoRepository

MAX_ITEMS = 200
MAX_TITLE_LENGTH = 240
MAX_DESCRIPTION_LENGTH = 4_000
MAX_DEPENDENCIES = 20
MAX_CHECKPOINTS = 100
MAX_CHECKPOINT_LABEL_LENGTH = 120

_ALLOWED_TRANSITIONS: dict[TodoStatus, frozenset[TodoStatus]] = {
    TodoStatus.PENDING: frozenset(
        {
            TodoStatus.PENDING,
            TodoStatus.IN_PROGRESS,
            TodoStatus.COMPLETED,
            TodoStatus.FAILED,
            TodoStatus.BLOCKED,
            TodoStatus.CANCELLED,
        }
    ),
    TodoStatus.IN_PROGRESS: frozenset(
        {
            TodoStatus.IN_PROGRESS,
            TodoStatus.COMPLETED,
            TodoStatus.FAILED,
            TodoStatus.BLOCKED,
            TodoStatus.CANCELLED,
        }
    ),
    TodoStatus.FAILED: frozenset(
        {TodoStatus.FAILED, TodoStatus.PENDING, TodoStatus.IN_PROGRESS, TodoStatus.CANCELLED}
    ),
    TodoStatus.BLOCKED: frozenset({TodoStatus.BLOCKED, TodoStatus.PENDING, TodoStatus.CANCELLED}),
    TodoStatus.COMPLETED: frozenset({TodoStatus.COMPLETED}),
    TodoStatus.CANCELLED: frozenset({TodoStatus.CANCELLED, TodoStatus.PENDING}),
}


class TodoService:
    """Transactional Todo application service.

    The service is intentionally synchronous: every operation is a short
    SQLite transaction and the caller remains responsible for async scheduling.
    ``mutation_count`` is an in-process observation counter used by the query
    loop; the database remains the durable source of truth.
    """

    def __init__(
        self,
        repository: TodoRepository,
        session_id: str,
        *,
        actor_id: str = "agent",
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        self.repository = repository
        self.session_id = session_id
        self.actor_id = actor_id or "agent"
        self._mutation_count = 0

    @property
    def mutation_count(self) -> int:
        """Return the number of successful mutations observed by this service."""
        return self._mutation_count

    def snapshot(self) -> TodoSnapshot | None:
        with self.repository.connection() as conn:
            return self.repository.load_snapshot(conn, self.session_id)

    def context_snapshot(self, *, max_chars: int = 8_000) -> str | None:
        current = self.snapshot()
        return current.render(max_chars=max_chars) if current is not None else None

    def claim_next(
        self,
        owner_id: str,
        *,
        lease_seconds: float = 30.0,
        preferred_item_id: str | None = None,
    ) -> TodoClaim | None:
        """Atomically claim one dependency-ready task for a Worker.

        Todo task ownership is deliberately separate from plan-version
        concurrency.  The claim transaction serializes competing Workers and
        records a lease so a crashed Worker does not own a task forever.
        """
        owner_id = self._validate_id(owner_id)
        lease_until = self._lease_deadline(lease_seconds)
        # A Worker should be able to recover an abandoned task immediately;
        # the manager heartbeat remains the periodic safety net.
        self.reap_expired_claims()
        now = self._now()
        with self.repository.transaction() as conn:
            current = self._require_plan(conn)
            items = current.item_map

            candidates: tuple[TodoItem, ...]
            if preferred_item_id is not None:
                preferred_item_id = self._validate_id(preferred_item_id)
                selected = items.get(preferred_item_id)
                if selected is None:
                    raise TodoValidationError(f"Todo item not found: {preferred_item_id}")
                if (
                    selected.owner_id == owner_id
                    and selected.status is TodoStatus.IN_PROGRESS
                    and not self._lease_expired(selected.lease_until, now)
                ):
                    return TodoClaim(
                        item=selected,
                        plan_version=current.plan_version,
                        owner_id=owner_id,
                        lease_until=selected.lease_until or lease_until,
                    )
                if selected.effective_status(items) != "ready":
                    raise TodoDependencyError(
                        f"Todo item is not ready: {preferred_item_id}"
                    )
                candidates = (selected,)
            else:
                candidates = tuple(
                    item
                    for item in current.items
                    if item.effective_status(items) == "ready"
                    and (
                        item.owner_id is None
                        or self._lease_expired(item.lease_until, now)
                    )
                )
            if not candidates:
                return None

            selected = candidates[0]
            updated = replace(
                selected,
                status=TodoStatus.IN_PROGRESS,
                blocked_reason=None,
                owner_id=owner_id,
                lease_until=lease_until,
                revision=selected.revision + 1,
                updated_at=now,
            )
            self.repository.update_item(
                conn,
                session_id=self.session_id,
                item=updated,
            )
            version = current.plan_version + 1
            self.repository.bump_plan_version(conn, self.session_id, version)
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=version,
                actor_id=self.actor_id,
                kind="item_claimed",
                payload={
                    "item_id": updated.id,
                    "owner_id": owner_id,
                    "lease_until": lease_until,
                },
            )

        self._mutation_count += 1
        return TodoClaim(
            item=updated,
            plan_version=version,
            owner_id=owner_id,
            lease_until=lease_until,
        )

    def renew_owner(self, owner_id: str, *, lease_seconds: float = 30.0) -> int:
        """Renew all active task leases held by one Worker."""
        owner_id = self._validate_id(owner_id)
        lease_until = self._lease_deadline(lease_seconds)
        now = self._now()
        with self.repository.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE todo_items
                SET lease_until = ?, updated_at = ?
                WHERE session_id = ? AND owner_id = ?
                  AND status IN ('in_progress', 'completed')
                """,
                (lease_until, now, self.session_id, owner_id),
            )
            return cursor.rowcount

    def release_owner(self, owner_id: str, *, requeue: bool = True) -> tuple[str, ...]:
        """Release all tasks held by a Worker, optionally putting work back."""
        owner_id = self._validate_id(owner_id)
        now = self._now()
        released: list[str] = []
        changed = False
        with self.repository.transaction() as conn:
            rows = conn.execute(
                "SELECT id, status, revision FROM todo_items "
                "WHERE session_id = ? AND owner_id = ?",
                (self.session_id, owner_id),
            ).fetchall()
            for raw_id, raw_status, raw_revision in rows:
                item_id = str(raw_id)
                status = TodoStatus(str(raw_status))
                next_status = (
                    TodoStatus.PENDING
                    if requeue and status in {TodoStatus.IN_PROGRESS, TodoStatus.COMPLETED}
                    else status
                )
                next_revision = int(raw_revision) + (1 if next_status is not status else 0)
                conn.execute(
                    "UPDATE todo_items SET status = ?, owner_id = NULL, lease_until = NULL, "
                    "revision = ?, updated_at = ? WHERE id = ? AND session_id = ?",
                    (next_status.value, next_revision, now, item_id, self.session_id),
                )
                released.append(item_id)
                changed = changed or next_status is not status
            if changed:
                version = self._bump_and_event(
                    conn,
                    kind="worker_tasks_released",
                    payload={"owner_id": owner_id, "item_ids": released},
                )
                del version
        if changed:
            self._mutation_count += 1
        return tuple(released)

    def reap_expired_claims(self) -> tuple[str, ...]:
        """Requeue task claims whose Worker lease has expired."""
        now = self._now()
        expired: list[str] = []
        changed = False
        with self.repository.transaction() as conn:
            rows = conn.execute(
                "SELECT id, status, revision FROM todo_items "
                "WHERE session_id = ? AND owner_id IS NOT NULL "
                "AND lease_until IS NOT NULL AND lease_until < ?",
                (self.session_id, now),
            ).fetchall()
            for raw_id, raw_status, raw_revision in rows:
                item_id = str(raw_id)
                status = TodoStatus(str(raw_status))
                next_status = (
                    TodoStatus.PENDING
                    if status in {TodoStatus.IN_PROGRESS, TodoStatus.COMPLETED}
                    else status
                )
                next_revision = int(raw_revision) + (1 if next_status is not status else 0)
                conn.execute(
                    "UPDATE todo_items SET status = ?, owner_id = NULL, lease_until = NULL, "
                    "revision = ?, updated_at = ? WHERE id = ? AND session_id = ?",
                    (next_status.value, next_revision, now, item_id, self.session_id),
                )
                expired.append(item_id)
                changed = changed or next_status is not status
            if changed:
                self._bump_and_event(
                    conn,
                    kind="expired_claims_reaped",
                    payload={"item_ids": expired},
                )
        if changed:
            self._mutation_count += 1
        return tuple(expired)

    def initialize(self) -> TodoSnapshot:
        """Create the one active plan for this session."""
        with self.repository.transaction() as conn:
            if self.repository.load_snapshot(conn, self.session_id) is not None:
                raise TodoPlanExistsError("this session already has an active Todo plan")
            session = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (self.session_id,)
            ).fetchone()
            if session is None:
                raise TodoValidationError(f"session does not exist: {self.session_id}")
            self.repository.create_plan(conn, self.session_id)
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=0,
                actor_id=self.actor_id,
                kind="plan_initialized",
                payload={},
            )
        self._mutation_count += 1
        current = self.snapshot()
        if current is None:  # pragma: no cover - transaction guarantees this
            raise TodoError("Todo plan disappeared after initialization")
        return current

    def add(
        self,
        title: str,
        *,
        description: str = "",
        dependencies: Iterable[str] = (),
        expected_version: int | None = None,
    ) -> TodoSnapshot:
        title = self._validate_title(title)
        description = self._validate_description(description)
        dependency_ids = self._normalize_dependencies(dependencies)
        with self.repository.transaction() as conn:
            current = self._require_plan(conn)
            self._check_version(current, expected_version)
            if len(current.items) >= MAX_ITEMS:
                raise TodoValidationError(f"a plan cannot contain more than {MAX_ITEMS} items")
            missing = sorted(set(dependency_ids) - set(current.item_map))
            if missing:
                raise TodoDependencyError(f"unknown dependencies: {', '.join(missing)}")
            now = self._now()
            item = TodoItem(
                id=str(uuid.uuid4()),
                title=title,
                description=description,
                status=TodoStatus.PENDING,
                blocked_reason=None,
                position=max((item.position for item in current.items), default=-1) + 1,
                revision=0,
                created_at=now,
                updated_at=now,
                dependencies=dependency_ids,
            )
            self.repository.add_item(conn, session_id=self.session_id, item=item)
            version = current.plan_version + 1
            self.repository.bump_plan_version(conn, self.session_id, version)
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=version,
                actor_id=self.actor_id,
                kind="item_added",
                payload={"item_id": item.id, "title": item.title},
            )
        self._mutation_count += 1
        return self._snapshot_or_error()

    def update(
        self,
        item_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: TodoStatus | str | None = None,
        dependencies: Iterable[str] | None = None,
        expected_version: int | None = None,
        expected_revision: int | None = None,
        owner_id: str | None = None,
    ) -> TodoSnapshot:
        item_id = self._validate_id(item_id)
        if title is not None:
            title = self._validate_title(title)
        if description is not None:
            description = self._validate_description(description)
        new_status = self._coerce_status(status) if status is not None else None
        dependency_ids = (
            self._normalize_dependencies(dependencies) if dependencies is not None else None
        )

        with self.repository.transaction() as conn:
            current = self._require_plan(conn)
            self._check_version(current, expected_version)
            existing = current.item_map.get(item_id)
            if existing is None:
                raise TodoValidationError(f"Todo item not found: {item_id}")
            if expected_revision is not None and existing.revision != expected_revision:
                raise TodoConflictError(
                    f"item revision conflict: expected {expected_revision}, "
                    f"current {existing.revision}"
                )
            if owner_id is not None:
                self._check_owner(existing, owner_id)
            target_status = new_status or existing.status
            if target_status not in _ALLOWED_TRANSITIONS[existing.status]:
                raise TodoValidationError(
                    f"invalid status transition: {existing.status.value} -> {target_status.value}"
                )
            target_dependencies = (
                existing.dependencies if dependency_ids is None else dependency_ids
            )
            self._validate_dependencies(current, item_id, target_dependencies)
            target_title = title if title is not None else existing.title
            target_description = description if description is not None else existing.description
            target_reason = "manual" if target_status is TodoStatus.BLOCKED else None
            if (
                target_title == existing.title
                and target_description == existing.description
                and target_status is existing.status
                and target_reason == existing.blocked_reason
                and target_dependencies == existing.dependencies
            ):
                return current
            target_owner = existing.owner_id
            target_lease = existing.lease_until
            if owner_id is None and target_status in {
                TodoStatus.PENDING,
                TodoStatus.COMPLETED,
                TodoStatus.CANCELLED,
            }:
                target_owner = None
                target_lease = None
            updated = replace(
                existing,
                title=target_title,
                description=target_description,
                status=target_status,
                blocked_reason=target_reason,
                revision=existing.revision + 1,
                updated_at=self._now(),
                dependencies=target_dependencies,
                owner_id=target_owner,
                lease_until=target_lease,
            )
            self.repository.update_item(conn, session_id=self.session_id, item=updated)
            version = current.plan_version + 1
            self.repository.bump_plan_version(conn, self.session_id, version)
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=version,
                actor_id=self.actor_id,
                kind="item_updated",
                payload={"item_id": item_id, "status": target_status.value},
            )
        self._mutation_count += 1
        return self._snapshot_or_error()

    def remove(self, item_id: str, *, expected_version: int | None = None) -> TodoSnapshot:
        item_id = self._validate_id(item_id)
        with self.repository.transaction() as conn:
            current = self._require_plan(conn)
            self._check_version(current, expected_version)
            if item_id not in current.item_map:
                raise TodoValidationError(f"Todo item not found: {item_id}")
            dependents = [
                item.id
                for item in current.items
                if item_id in item.dependencies and item.id != item_id
            ]
            if dependents:
                raise TodoDependencyError(
                    f"cannot remove {item_id}; dependent items exist: {', '.join(dependents)}"
                )
            self.repository.remove_item(conn, self.session_id, item_id)
            version = current.plan_version + 1
            self.repository.bump_plan_version(conn, self.session_id, version)
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=version,
                actor_id=self.actor_id,
                kind="item_removed",
                payload={"item_id": item_id},
            )
        self._mutation_count += 1
        return self._snapshot_or_error()

    def create_checkpoint(
        self, label: str, *, expected_version: int | None = None
    ) -> TodoCheckpoint:
        """Persist an exact Todo snapshot without changing the plan version."""
        label = self._validate_checkpoint_label(label)
        with self.repository.transaction() as conn:
            current = self._require_plan(conn)
            self._check_version(current, expected_version)
            count = conn.execute(
                "SELECT COUNT(*) FROM todo_checkpoints WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            if count and int(count[0]) >= MAX_CHECKPOINTS:
                raise TodoValidationError(
                    f"a session cannot contain more than {MAX_CHECKPOINTS} checkpoints"
                )
            checkpoint_id = str(uuid.uuid4())
            self.repository.create_checkpoint(
                conn,
                checkpoint_id=checkpoint_id,
                session_id=self.session_id,
                label=label,
                source_plan_version=current.plan_version,
                snapshot=current.to_storage_dict(),
            )
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=current.plan_version,
                actor_id=self.actor_id,
                kind="checkpoint_created",
                payload={"checkpoint_id": checkpoint_id, "label": label},
            )
            checkpoint = self.repository.load_checkpoint(conn, self.session_id, checkpoint_id)
            if checkpoint is None:  # pragma: no cover - transaction guarantees this
                raise TodoError("checkpoint disappeared after creation")
        self._mutation_count += 1
        return checkpoint[0]

    def list_checkpoints(self, *, limit: int = 50) -> tuple[TodoCheckpoint, ...]:
        if limit < 1 or limit > MAX_CHECKPOINTS:
            raise TodoValidationError(f"limit must be between 1 and {MAX_CHECKPOINTS}")
        with self.repository.connection() as conn:
            return self.repository.list_checkpoints(conn, self.session_id, limit)

    def rollback(
        self,
        checkpoint_id: str,
        *,
        expected_version: int | None = None,
    ) -> TodoSnapshot:
        """Restore only Todo rows from a same-session checkpoint."""
        checkpoint_id = self._validate_id(checkpoint_id)
        with self.repository.transaction() as conn:
            current = self._require_plan(conn)
            self._check_version(current, expected_version)
            checkpoint = self.repository.load_checkpoint(conn, self.session_id, checkpoint_id)
            if checkpoint is None:
                raise TodoValidationError(f"checkpoint not found: {checkpoint_id}")
            _, payload = checkpoint
            items = self._decode_checkpoint_items(payload, current)
            self.repository.replace_items(
                conn,
                session_id=self.session_id,
                items=items,
            )
            version = current.plan_version + 1
            self.repository.bump_plan_version(conn, self.session_id, version)
            self.repository.append_event(
                conn,
                session_id=self.session_id,
                plan_version=version,
                actor_id=self.actor_id,
                kind="checkpoint_rollback",
                payload={"checkpoint_id": checkpoint_id},
            )
        self._mutation_count += 1
        return self._snapshot_or_error()

    def _require_plan(self, conn: sqlite3.Connection) -> TodoSnapshot:
        current = self.repository.load_snapshot(conn, self.session_id)
        if current is None:
            raise TodoValidationError("Todo plan is not initialized; run todo_init first")
        return current

    def _snapshot_or_error(self) -> TodoSnapshot:
        current = self.snapshot()
        if current is None:  # pragma: no cover - transaction guarantees this
            raise TodoError("Todo plan disappeared after mutation")
        return current

    @staticmethod
    def _check_version(current: TodoSnapshot, expected: int | None) -> None:
        if expected is not None and expected != current.plan_version:
            raise TodoConflictError(
                f"plan version conflict: expected {expected}, current {current.plan_version}"
            )

    @staticmethod
    def _validate_title(title: str) -> str:
        value = title.strip()
        if not value:
            raise TodoValidationError("title must not be empty")
        if len(value) > MAX_TITLE_LENGTH:
            raise TodoValidationError(f"title must be at most {MAX_TITLE_LENGTH} characters")
        return value

    @staticmethod
    def _validate_description(description: str) -> str:
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise TodoValidationError(
                f"description must be at most {MAX_DESCRIPTION_LENGTH} characters"
            )
        return description

    @staticmethod
    def _validate_id(item_id: str) -> str:
        value = item_id.strip()
        if not value:
            raise TodoValidationError("item_id must not be empty")
        return value

    @staticmethod
    def _validate_checkpoint_label(label: str) -> str:
        value = label.strip()
        if not value:
            raise TodoValidationError("checkpoint label must not be empty")
        if len(value) > MAX_CHECKPOINT_LABEL_LENGTH:
            raise TodoValidationError(
                f"checkpoint label must be at most {MAX_CHECKPOINT_LABEL_LENGTH} characters"
            )
        return value

    @staticmethod
    def _normalize_dependencies(dependencies: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in dependencies if item.strip()))
        if len(normalized) > MAX_DEPENDENCIES:
            raise TodoValidationError(
                f"an item cannot have more than {MAX_DEPENDENCIES} dependencies"
            )
        return normalized

    @staticmethod
    def _coerce_status(status: TodoStatus | str) -> TodoStatus:
        try:
            return status if isinstance(status, TodoStatus) else TodoStatus(status)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in TodoStatus)
            raise TodoValidationError(f"invalid status {status!r}; expected: {allowed}") from exc

    @staticmethod
    def _validate_dependencies(
        current: TodoSnapshot,
        item_id: str,
        dependencies: tuple[str, ...],
    ) -> None:
        if item_id in dependencies:
            raise TodoDependencyError("an item cannot depend on itself")
        item_map = current.item_map
        missing = sorted(set(dependencies) - set(item_map))
        if missing:
            raise TodoDependencyError(f"unknown dependencies: {', '.join(missing)}")
        graph = {item.id: item.dependencies for item in current.items}
        graph[item_id] = dependencies
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise TodoDependencyError("dependency cycle detected")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph.get(node, ()):
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    @staticmethod
    def _check_owner(item: TodoItem, owner_id: str) -> None:
        if item.owner_id != owner_id:
            raise TodoConflictError(f"Todo item is owned by another Worker: {item.id}")
        if TodoService._lease_expired(item.lease_until, TodoService._now()):
            raise TodoConflictError(f"Todo item lease expired: {item.id}")

    @staticmethod
    def _lease_deadline(seconds: float) -> str:
        if seconds <= 0:
            raise TodoValidationError("lease_seconds must be positive")
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _lease_expired(value: str | None, now: str) -> bool:
        if value is None:
            return True
        try:
            return datetime.fromisoformat(value) <= datetime.fromisoformat(now)
        except ValueError:
            return True

    def _bump_and_event(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> int:
        current = self.repository.load_snapshot(conn, self.session_id)
        if current is None:  # pragma: no cover - guarded by callers
            raise TodoError("Todo plan is not initialized")
        version = current.plan_version + 1
        self.repository.bump_plan_version(conn, self.session_id, version)
        self.repository.append_event(
            conn,
            session_id=self.session_id,
            plan_version=version,
            actor_id=self.actor_id,
            kind=kind,
            payload=payload,
        )
        return version

    def _decode_checkpoint_items(
        self, payload: dict[str, Any], current: TodoSnapshot
    ) -> tuple[TodoItem, ...]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
            raise TodoValidationError("checkpoint contains an invalid item list")
        decoded: list[TodoItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise TodoValidationError("checkpoint contains an invalid item")
            try:
                raw_dependencies = raw.get("dependencies", [])
                if not isinstance(raw_dependencies, list):
                    raise TypeError("dependencies must be a list")
                position = int(raw["position"])
                revision = int(raw["revision"])
                if position < 0 or revision < 0:
                    raise ValueError("position and revision must be non-negative")
                item_id = self._validate_id(str(raw["id"]))
                status = self._coerce_status(str(raw["status"]))
                dependencies = self._normalize_dependencies(raw_dependencies)
                item = TodoItem(
                    id=item_id,
                    title=self._validate_title(str(raw["title"])),
                    description=self._validate_description(str(raw.get("description", ""))),
                    status=status,
                    blocked_reason=(
                        str(raw["blocked_reason"])
                        if raw.get("blocked_reason") is not None
                        else None
                    ),
                    owner_id=(str(raw["owner_id"]) if raw.get("owner_id") is not None else None),
                    lease_until=(
                        str(raw["lease_until"]) if raw.get("lease_until") is not None else None
                    ),
                    position=position,
                    revision=revision,
                    created_at=str(raw["created_at"]),
                    updated_at=str(raw["updated_at"]),
                    dependencies=dependencies,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise TodoValidationError("checkpoint contains malformed item data") from exc
            if item.id in seen:
                raise TodoValidationError(f"checkpoint contains duplicate item: {item.id}")
            seen.add(item.id)
            decoded.append(item)
        snapshot = TodoSnapshot(
            session_id=current.session_id,
            plan_version=current.plan_version,
            items=tuple(sorted(decoded, key=lambda item: (item.position, item.id))),
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        for item in snapshot.items:
            self._validate_dependencies(snapshot, item.id, item.dependencies)
        return snapshot.items

    @staticmethod
    def _now() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()
