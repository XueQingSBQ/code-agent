"""Bounded planning notices for caller-supplied execution budgets.

These notices are advisory. The host still owns cancellation/timeouts and the
query loop still enforces its configured call limit. No default limit is added.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionBudget:
    max_calls: int | None = None
    deadline: float | None = None
    _call_notices: set[int] = field(default_factory=set, init=False)
    _time_notices: set[int] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("deadline must be a finite event-loop clock value")

    def notice(self, *, calls_used: int, now: float) -> dict[str, Any] | None:
        """Warn at most twice per budget dimension, never on every request."""
        remaining_calls = None if self.max_calls is None else max(0, self.max_calls - calls_used)
        remaining_seconds = None if self.deadline is None else max(0.0, self.deadline - now)
        warn_calls = self._crossed(remaining_calls, (5, 1), self._call_notices)
        warn_time = self._crossed(remaining_seconds, (60, 15), self._time_notices)
        if not (warn_calls or warn_time):
            return None
        parts = []
        if remaining_calls is not None:
            parts.append(f"{remaining_calls} model call(s) remaining, including the upcoming call")
        if remaining_seconds is not None:
            parts.append(f"approximately {math.ceil(remaining_seconds)} seconds remaining")
        text = (
            "Execution budget: " + "; ".join(parts) + ". "
            "Prioritize the requested outcome and focused regression checks. "
            "Avoid optional exploration or broad test suites that cannot finish in this budget. "
            "Reserve a model call for an evidence-backed final response. "
            "If the fix is verified, summarize it now instead of making cosmetic changes or "
            "extra Todo/read calls. If verification is incomplete or failing, state that honestly; "
            "do not claim success or skip necessary checks just to finish. "
            "Tool results from the final allowed call cannot receive a follow-up model response."
        )
        return {
            "remaining_calls": remaining_calls,
            "remaining_seconds": remaining_seconds,
            "message": text,
        }

    @staticmethod
    def _crossed(value: float | None, thresholds: tuple[int, ...], seen: set[int]) -> bool:
        crossed = {
            threshold for threshold in thresholds if value is not None and value <= threshold
        }
        fresh = crossed - seen
        seen.update(crossed)
        return bool(fresh)
