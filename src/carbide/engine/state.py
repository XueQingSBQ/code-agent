"""Turn state management with frozen dataclasses.

Python lacks TS's compile-time immutability, so we use frozen dataclasses
to ensure cross-turn state is never mutated in place. Any modification
requires creating a new instance via replace().
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TurnState:
    """
    Immutable state that persists across turns.

    Use dataclass(frozen=True) to prevent accidental mutation.
    Any update requires: new_state = replace(old_state, field=new_value)
    """

    # Conversation history
    messages: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    # Current turn number
    turn_number: int = 0

    # Token usage tracking
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Workspace information
    workspace: Path = field(default_factory=Path.cwd)

    # Compression state
    last_compression_turn: int = 0
    compression_count: int = 0

    # Tool execution tracking
    total_tool_calls: int = 0
    failed_tool_calls: int = 0

    # Timestamps
    session_start_time: float = field(default_factory=time.time)
    last_turn_time: float = field(default_factory=time.time)

    # Exit state
    should_exit: bool = False
    exit_reason: str | None = None

    # Repair tracking (orphan tool_use detection)
    pending_tool_use_ids: frozenset[str] = field(default_factory=frozenset)

    # Plan mode state
    in_plan_mode: bool = False
    plan_content: str | None = None

    def add_message(self, message: dict[str, Any]) -> TurnState:
        """
        Add a message to history.

        Returns new state with updated messages.
        """
        new_messages = (*self.messages, message)
        return replace(self, messages=new_messages)

    def add_messages(self, messages: list[dict[str, Any]]) -> TurnState:
        """Add multiple messages to history."""
        new_messages = self.messages + tuple(messages)
        return replace(self, messages=new_messages)

    def replace_messages(self, messages: list[dict[str, Any]]) -> TurnState:
        """Replace entire message history (used after compression)."""
        return replace(self, messages=tuple(messages))

    def increment_turn(self) -> TurnState:
        """Move to next turn."""
        return replace(
            self,
            turn_number=self.turn_number + 1,
            last_turn_time=time.time(),
        )

    def update_tokens(self, input_tokens: int, output_tokens: int) -> TurnState:
        """Update token usage from API response."""
        return replace(
            self,
            total_input_tokens=self.total_input_tokens + input_tokens,
            total_output_tokens=self.total_output_tokens + output_tokens,
        )

    def record_compression(self) -> TurnState:
        """Record that compression occurred this turn."""
        return replace(
            self,
            last_compression_turn=self.turn_number,
            compression_count=self.compression_count + 1,
        )

    def record_tool_call(self, success: bool = True) -> TurnState:
        """Record tool execution."""
        if success:
            return replace(self, total_tool_calls=self.total_tool_calls + 1)
        else:
            return replace(
                self,
                total_tool_calls=self.total_tool_calls + 1,
                failed_tool_calls=self.failed_tool_calls + 1,
            )

    def mark_pending_tool_use(self, tool_use_id: str) -> TurnState:
        """Mark a tool_use as pending (waiting for result)."""
        new_pending = self.pending_tool_use_ids | {tool_use_id}
        return replace(self, pending_tool_use_ids=frozenset(new_pending))

    def mark_tool_use_completed(self, tool_use_id: str) -> TurnState:
        """Mark a tool_use as completed."""
        new_pending = self.pending_tool_use_ids - {tool_use_id}
        return replace(self, pending_tool_use_ids=frozenset(new_pending))

    def mark_exit(self, reason: str) -> TurnState:
        """Mark state for exit."""
        return replace(
            self,
            should_exit=True,
            exit_reason=reason,
        )

    def enter_plan_mode(self, plan_content: str | None = None) -> TurnState:
        """Enter plan mode."""
        return replace(
            self,
            in_plan_mode=True,
            plan_content=plan_content,
        )

    def exit_plan_mode(self) -> TurnState:
        """Exit plan mode."""
        return replace(
            self,
            in_plan_mode=False,
            plan_content=None,
        )

    def get_session_duration(self) -> float:
        """Get session duration in seconds."""
        return time.time() - self.session_start_time

    def get_turns_since_compression(self) -> int:
        """Get number of turns since last compression."""
        return self.turn_number - self.last_compression_turn

    def get_failure_rate(self) -> float:
        """Get tool call failure rate."""
        if self.total_tool_calls == 0:
            return 0.0
        return self.failed_tool_calls / self.total_tool_calls

    def to_dict(self) -> dict[str, Any]:
        """Convert state to dictionary for serialization."""
        return {
            "turn_number": self.turn_number,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "workspace": str(self.workspace),
            "last_compression_turn": self.last_compression_turn,
            "compression_count": self.compression_count,
            "total_tool_calls": self.total_tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "session_start_time": self.session_start_time,
            "last_turn_time": self.last_turn_time,
            "should_exit": self.should_exit,
            "exit_reason": self.exit_reason,
            "pending_tool_use_ids": list(self.pending_tool_use_ids),
            "in_plan_mode": self.in_plan_mode,
            "message_count": len(self.messages),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], messages: list[dict[str, Any]]) -> TurnState:
        """Restore state from dictionary."""
        return cls(
            messages=tuple(messages),
            turn_number=data.get("turn_number", 0),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            workspace=Path(data.get("workspace", ".")),
            last_compression_turn=data.get("last_compression_turn", 0),
            compression_count=data.get("compression_count", 0),
            total_tool_calls=data.get("total_tool_calls", 0),
            failed_tool_calls=data.get("failed_tool_calls", 0),
            session_start_time=data.get("session_start_time", time.time()),
            last_turn_time=data.get("last_turn_time", time.time()),
            should_exit=data.get("should_exit", False),
            exit_reason=data.get("exit_reason"),
            pending_tool_use_ids=frozenset(data.get("pending_tool_use_ids", [])),
            in_plan_mode=data.get("in_plan_mode", False),
            plan_content=data.get("plan_content"),
        )
