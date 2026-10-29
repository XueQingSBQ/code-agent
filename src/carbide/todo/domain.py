"""Immutable Todo domain values and deterministic snapshot rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TodoStatus(StrEnum):
    """Persisted task states; ``ready`` and dependency-blocked are computed."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TodoItem:
    id: str
    title: str
    description: str
    status: TodoStatus
    blocked_reason: str | None
    position: int
    revision: int
    created_at: str
    updated_at: str
    dependencies: tuple[str, ...] = ()
    owner_id: str | None = None
    lease_until: str | None = None

    def effective_status(self, items: dict[str, TodoItem]) -> str:
        """Return the display status without persisting derived state."""
        if self.status is not TodoStatus.PENDING:
            return self.status.value
        if any(
            dependency_id not in items or items[dependency_id].status is not TodoStatus.COMPLETED
            for dependency_id in self.dependencies
        ):
            return "blocked"
        return "ready"

    def to_dict(self, items: dict[str, TodoItem]) -> dict[str, Any]:
        """Return a stable API representation."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "effective_status": self.effective_status(items),
            "blocked_reason": self.blocked_reason,
            "owner_id": self.owner_id,
            "lease_until": self.lease_until,
            "dependencies": list(self.dependencies),
            "revision": self.revision,
            "position": self.position,
        }


@dataclass(frozen=True, slots=True)
class TodoSnapshot:
    """Consistent read view of one session's Todo plan."""

    session_id: str
    plan_version: int
    items: tuple[TodoItem, ...]
    created_at: str
    updated_at: str

    @property
    def item_map(self) -> dict[str, TodoItem]:
        return {item.id: item for item in self.items}

    def to_dict(self) -> dict[str, Any]:
        items = self.item_map
        return {
            "session_id": self.session_id,
            "plan_version": self.plan_version,
            "items": [item.to_dict(items) for item in self.items],
            "updated_at": self.updated_at,
        }

    def to_storage_dict(self) -> dict[str, Any]:
        """Return the raw state needed for an exact Todo-only restore."""
        return {
            "plan_version": self.plan_version,
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "description": item.description,
                    "status": item.status.value,
                    "blocked_reason": item.blocked_reason,
                    "owner_id": item.owner_id,
                    "lease_until": item.lease_until,
                    "position": item.position,
                    "revision": item.revision,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "dependencies": list(item.dependencies),
                }
                for item in self.items
            ],
        }

    def render(self, *, max_chars: int = 12_000) -> str:
        """Render a bounded, model-friendly snapshot."""
        lines = [f"Todo plan (version {self.plan_version})"]
        items = self.item_map
        if not self.items:
            lines.append("(empty)")
        for item in self.items:
            status = item.effective_status(items)
            dependency_text = (
                f"; depends_on={','.join(item.dependencies)}" if item.dependencies else ""
            )
            lines.append(f"- [{item.id}] {status}: {item.title}{dependency_text}")
            if item.description:
                lines.append(f"  {item.description}")
        rendered = "\n".join(lines)
        if len(rendered) <= max_chars:
            return rendered
        return rendered[: max_chars - 64].rstrip() + "\n...[Todo snapshot truncated]"


@dataclass(frozen=True, slots=True)
class TodoClaim:
    """A task lease granted to one Agent worker."""

    item: TodoItem
    plan_version: int
    owner_id: str
    lease_until: str

    def to_dict(self, items: dict[str, TodoItem] | None = None) -> dict[str, Any]:
        """Return the stable model-facing claim payload."""
        item_map = items or {self.item.id: self.item}
        return {
            "item": self.item.to_dict(item_map),
            "plan_version": self.plan_version,
            "owner_id": self.owner_id,
            "lease_until": self.lease_until,
        }


@dataclass(frozen=True, slots=True)
class TodoCheckpoint:
    """Metadata for a durable Todo-only checkpoint."""

    id: str
    label: str
    source_plan_version: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "source_plan_version": self.source_plan_version,
            "created_at": self.created_at,
        }
