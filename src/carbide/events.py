"""The event vocabulary shared by engine, SDK and CLI.

The engine yields these; every surface (CLI, SDK consumer) reacts to them.
Keeping this module dependency-free is what lets the engine stay ignorant of
any UI: it emits facts, someone else decides how to draw them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ExitReason(StrEnum):
    """Why the loop stopped.

    Only ``COMPLETED`` is the happy path. Everything else exists because
    something went wrong and the caller may want to retry or explain.
    """

    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    ABORTED = "aborted"
    PROMPT_TOO_LONG = "prompt_too_long"
    OUTPUT_TRUNCATION_UNRECOVERED = "output_truncation_unrecovered"
    PROVIDER_ERROR = "provider_error"
    COMPACTION_FAILED = "compaction_failed"


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A chunk of assistant prose, already streaming to the user."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """A chunk of extended-thinking output."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """A tool_use block finished parsing and has been dispatched.

    Emitted the moment dispatch happens, which is *before* the model has
    finished streaming — see StreamingToolExecutor.
    """

    tool_use_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    tool_use_id: str
    name: str
    is_error: bool
    duration_ms: float
    preview: str


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """A model turn has started and a request is about to be sent."""

    turn: int


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    """The loop is blocked until a surface answers this."""

    tool_use_id: str
    name: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class CompactionOccurred:
    """A context-pyramid layer fired. Fuel for the M1 efficiency metric."""

    layer: Literal["spill", "snip", "micro", "compact", "restore"]
    tokens_before: int
    tokens_after: int
    detail: str = ""

    @property
    def tokens_freed(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    turn: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass(frozen=True, slots=True)
class Warning:
    """Something degraded but the loop continues (retry, repair, truncation)."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Done:
    reason: ExitReason
    turns: int
    usage: dict[str, int] = field(default_factory=dict)


Event = (
    TextDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolCallFinished
    | TurnStarted
    | PermissionRequested
    | CompactionOccurred
    | TurnCompleted
    | Warning
    | Done
)
