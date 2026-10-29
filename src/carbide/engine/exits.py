"""Exit reason handling and cleanup logic.

Defines all possible exit reasons and their associated cleanup actions.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any


class ExitReason(StrEnum):
    """
    Enumeration of all possible exit reasons.

    Each reason may have different cleanup requirements.
    """

    # Normal completion
    TASK_COMPLETE = "task_complete"
    USER_REQUEST = "user_request"

    # Token budget exhausted
    TOKEN_LIMIT = "token_limit"
    TURN_LIMIT = "turn_limit"

    # Errors
    API_ERROR = "api_error"
    TOOL_ERROR = "tool_error"
    PERMISSION_DENIED = "permission_denied"

    # Interruption
    USER_INTERRUPT = "user_interrupt"  # Ctrl+C
    TIMEOUT = "timeout"

    # Circuit breaker
    FAILURE_THRESHOLD = "failure_threshold"  # Too many failures

    # Model behavior
    NO_PROGRESS = "no_progress"  # Stuck in loop
    SAFETY_STOP = "safety_stop"  # Safety filter triggered


class ExitHandler:
    """
    Handles exit cleanup and reporting based on exit reason.
    """

    def __init__(self) -> None:
        self._cleanup_handlers: dict[ExitReason, Callable[[dict[str, Any]], Any]] = {}

    def register_cleanup(
        self, reason: ExitReason, handler: Callable[[dict[str, Any]], Any]
    ) -> None:
        """
        Register a cleanup handler for specific exit reason.

        Args:
            reason: Exit reason to handle
            handler: Cleanup function (can be async)
        """
        self._cleanup_handlers[reason] = handler

    async def handle_exit(
        self,
        reason: ExitReason,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Handle exit with appropriate cleanup.

        Args:
            reason: Why we're exiting
            context: Current execution context

        Returns:
            Exit report with statistics and cleanup results
        """
        report = {
            "reason": reason.value,
            "message": self._get_exit_message(reason),
            "should_save_state": self._should_save_state(reason),
            "is_error": self._is_error_exit(reason),
        }

        # Run cleanup handler if registered
        if reason in self._cleanup_handlers:
            handler = self._cleanup_handlers[reason]
            try:
                if callable(handler):
                    cleanup_result = handler(context)
                    # Handle async handlers
                    if hasattr(cleanup_result, "__await__"):
                        cleanup_result = await cleanup_result
                    report["cleanup"] = cleanup_result
            except Exception as e:
                report["cleanup_error"] = str(e)

        return report

    def _get_exit_message(self, reason: ExitReason) -> str:
        """Get human-readable exit message."""
        messages = {
            ExitReason.TASK_COMPLETE: "Task completed successfully.",
            ExitReason.USER_REQUEST: "Exiting at user request.",
            ExitReason.TOKEN_LIMIT: "Token budget exhausted.",
            ExitReason.TURN_LIMIT: "Maximum turn limit reached.",
            ExitReason.API_ERROR: "API error occurred, cannot continue.",
            ExitReason.TOOL_ERROR: "Critical tool error occurred.",
            ExitReason.PERMISSION_DENIED: "Permission denied, cannot proceed.",
            ExitReason.USER_INTERRUPT: "Interrupted by user (Ctrl+C).",
            ExitReason.TIMEOUT: "Operation timed out.",
            ExitReason.FAILURE_THRESHOLD: "Too many failures, circuit breaker triggered.",
            ExitReason.NO_PROGRESS: "No progress detected, possible infinite loop.",
            ExitReason.SAFETY_STOP: "Safety filter triggered, stopping execution.",
        }
        return messages.get(reason, f"Exiting: {reason.value}")

    def _should_save_state(self, reason: ExitReason) -> bool:
        """
        Determine if state should be saved for resume.

        Args:
            reason: Exit reason

        Returns:
            True if state should be persisted for --resume
        """
        # Save state for most exits except critical errors
        dont_save = {
            ExitReason.SAFETY_STOP,
            ExitReason.PERMISSION_DENIED,
        }
        return reason not in dont_save

    def _is_error_exit(self, reason: ExitReason) -> bool:
        """Check if exit is due to error."""
        error_reasons = {
            ExitReason.API_ERROR,
            ExitReason.TOOL_ERROR,
            ExitReason.TIMEOUT,
            ExitReason.FAILURE_THRESHOLD,
            ExitReason.NO_PROGRESS,
            ExitReason.SAFETY_STOP,
        }
        return reason in error_reasons

    def create_exit_summary(
        self,
        reason: ExitReason,
        state: Any,  # TurnState
    ) -> dict[str, Any]:
        """
        Create comprehensive exit summary.

        Args:
            reason: Exit reason
            state: Final turn state

        Returns:
            Summary dictionary with statistics
        """
        return {
            "exit_reason": reason.value,
            "exit_message": self._get_exit_message(reason),
            "statistics": {
                "total_turns": state.turn_number,
                "total_input_tokens": state.total_input_tokens,
                "total_output_tokens": state.total_output_tokens,
                "total_tokens": state.total_input_tokens + state.total_output_tokens,
                "total_tool_calls": state.total_tool_calls,
                "failed_tool_calls": state.failed_tool_calls,
                "failure_rate": state.get_failure_rate(),
                "compression_count": state.compression_count,
                "session_duration_seconds": state.get_session_duration(),
            },
            "final_state": {
                "message_count": len(state.messages),
                "workspace": str(state.workspace),
                "in_plan_mode": state.in_plan_mode,
                "pending_tool_uses": len(state.pending_tool_use_ids),
            },
            "resumable": self._should_save_state(reason),
            "is_error": self._is_error_exit(reason),
        }


def check_exit_conditions(state: Any) -> ExitReason | None:
    """
    Check if any exit conditions are met.

    Args:
        state: Current turn state

    Returns:
        ExitReason if should exit, None otherwise
    """
    # Check explicit exit flag
    if state.should_exit:
        return ExitReason.USER_REQUEST

    # Check failure threshold (circuit breaker)
    if state.total_tool_calls > 10 and state.get_failure_rate() > 0.5:
        return ExitReason.FAILURE_THRESHOLD

    # Check turn limit (safety)
    if state.turn_number > 100:
        return ExitReason.TURN_LIMIT

    # Check for no progress (same message repeated)
    if len(state.messages) > 10:
        recent = list(state.messages[-5:])
        # Simple heuristic: check if last 2 assistant messages are identical
        assistant_msgs = [m for m in recent if m.get("role") == "assistant"]
        if len(assistant_msgs) >= 2:
            last_two = assistant_msgs[-2:]
            if last_two[0].get("content") == last_two[1].get("content"):
                return ExitReason.NO_PROGRESS

    return None
