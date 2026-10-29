"""Stable domain contracts for parent-child Agent execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SubagentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_PARENT = "waiting_for_parent"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MERGE_BLOCKED = "merge_blocked"
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class SubagentRequest:
    """A request to create one generic Todo Worker.

    Every child is the same generic Worker.  It chooses a ready task from the
    shared parent-session Todo plan instead of receiving a task assignment.
    """

    task: str
    background: bool = False
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("Subagent task must not be empty")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """One durable message on the direct parent-child channel."""

    message_id: str
    run_id: str
    direction: str
    kind: str
    content: Any
    status: str
    created_at: str
    delivered_at: str | None = None
    acknowledged_at: str | None = None


@dataclass(slots=True)
class SubagentResult:
    """Full child result plus a short UI-oriented summary."""

    agent_id: str
    parent_session_id: str
    status: SubagentStatus
    final_text: str = ""
    summary: str = ""
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    child_session_id: str | None = None
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """Return the complete model-facing result payload."""
        return {
            "agent_id": self.agent_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "status": self.status.value,
            "result": {
                "final_text": self.final_text,
            },
            "summary": self.summary or self.final_text[:240],
            "error": self.error,
            "usage": self.usage,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
        }

    def to_tool_result(self) -> str:
        """Serialize the full result for a parent tool_result block."""
        return json.dumps(self.payload(), ensure_ascii=False)
