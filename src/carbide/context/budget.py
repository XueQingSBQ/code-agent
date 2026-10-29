"""Token budget management with three-tier calibration.

Solves the Python stack's token counting problem: no local Anthropic tokenizer.
Uses approximation + periodic API calibration + usage-based correction.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenEstimate:
    """Token count estimate with confidence level."""

    tokens: int
    method: str  # 'approximate' | 'api_calibrated' | 'usage_actual'
    confidence: float  # 0.0-1.0


@dataclass
class CalibrationData:
    """Calibration coefficients from API feedback."""

    # Correction factor: actual_tokens = approximate * coefficient
    coefficient: float = 1.0

    # Number of samples used for calibration
    sample_count: int = 0

    # Last calibration timestamp
    last_calibrated: float = field(default_factory=time.time)

    # Running error metrics
    mean_error: float = 0.0  # avg(actual - approximate)
    max_error: float = 0.0


class TokenBudget:
    """
    Manages token budget and tracks usage.

    Three-tier strategy:
    1. Hot path: local approximation (~4 chars/token)
    2. Periodic: API count_tokens() every N turns
    3. Reactive: usage.input_tokens from last response
    """

    def __init__(
        self,
        max_tokens: int = 200_000,
        compression_threshold: float = 0.8,
        calibration_interval: int = 10,  # Calibrate every N turns
    ):
        self.max_tokens = max_tokens
        self.compression_threshold = compression_threshold
        self.calibration_interval = calibration_interval

        self.current_usage = 0
        self.turn_count = 0
        self.calibration = CalibrationData()

    def estimate_tokens(
        self,
        content: str | dict[str, Any] | list[Any],
        method: str = "approximate",
    ) -> TokenEstimate:
        """
        Estimate token count for content.

        Args:
            content: String or structured content
            method: 'approximate' | 'calibrated'
        """
        char_count = len(content) if isinstance(content, str) else len(str(content))

        # Base approximation: ~4 chars per token
        approximate = char_count // 4

        if method == "calibrated":
            # Apply learned coefficient
            tokens = int(approximate * self.calibration.coefficient)
            confidence = min(0.9, 0.5 + (self.calibration.sample_count / 100))
        else:
            tokens = approximate
            confidence = 0.5  # Low confidence for raw approximation

        return TokenEstimate(
            tokens=tokens,
            method=method,
            confidence=confidence,
        )

    def update_from_api_count(
        self,
        actual_tokens: int,
        content: str | dict[str, Any] | list[Any],
    ) -> None:
        """
        Calibrate using API count_tokens() result (Tier 2).

        This is called periodically (every N turns) to refine the coefficient.
        """
        approximate = self.estimate_tokens(content, method="approximate").tokens

        if approximate > 0:
            # Update coefficient with exponential moving average
            new_coefficient = actual_tokens / approximate
            alpha = 0.3  # Smoothing factor

            self.calibration.coefficient = (
                alpha * new_coefficient + (1 - alpha) * self.calibration.coefficient
            )

            # Update error metrics
            error = actual_tokens - approximate
            self.calibration.mean_error = alpha * error + (1 - alpha) * self.calibration.mean_error
            self.calibration.max_error = max(self.calibration.max_error, abs(error))

            self.calibration.sample_count += 1
            self.calibration.last_calibrated = time.time()

    def update_from_usage(self, usage_tokens: int) -> None:
        """
        Update with authoritative usage from API response (Tier 3).

        This is the most accurate signal and is used to correct drift.
        """
        self.current_usage = usage_tokens
        self.turn_count += 1

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """Check if compression should be triggered."""
        estimated = sum(self.estimate_tokens(msg, method="calibrated").tokens for msg in messages)

        return estimated > self.max_tokens * self.compression_threshold

    def should_calibrate(self) -> bool:
        """Check if API calibration is due."""
        return self.turn_count % self.calibration_interval == 0

    def get_usage_ratio(self) -> float:
        """Get current usage as ratio of max tokens."""
        return self.current_usage / self.max_tokens if self.max_tokens > 0 else 0.0

    def get_remaining_tokens(self) -> int:
        """Get remaining token budget."""
        return max(0, self.max_tokens - self.current_usage)

    def get_calibration_stats(self) -> dict[str, Any]:
        """Get calibration statistics for observability."""
        return {
            "coefficient": self.calibration.coefficient,
            "sample_count": self.calibration.sample_count,
            "mean_error": self.calibration.mean_error,
            "max_error": self.calibration.max_error,
            "last_calibrated_ago_seconds": time.time() - self.calibration.last_calibrated,
        }


class TokenCalibrator:
    """
    Helper for periodic API-based calibration.

    Usage:
        calibrator = TokenCalibrator(api_client, budget)
        await calibrator.calibrate_if_needed(messages)
    """

    def __init__(self, budget: TokenBudget) -> None:
        self.budget = budget

    async def calibrate_if_needed(
        self,
        messages: list[dict[str, Any]],
        count_tokens_func: Callable[[list[dict[str, Any]]], Awaitable[int]],
    ) -> bool:
        """
        Calibrate if needed using API count_tokens().

        Returns:
            True if calibration was performed
        """
        if not self.budget.should_calibrate():
            return False

        try:
            actual_tokens = await count_tokens_func(messages)
            self.budget.update_from_api_count(actual_tokens, messages)
            return True
        except Exception:
            # Calibration is best-effort, don't fail the main path
            return False

    def calibrate_from_content(
        self,
        content: str | dict[str, Any] | list[Any],
        actual_tokens: int,
    ) -> None:
        """Directly calibrate from known actual token count."""
        self.budget.update_from_api_count(actual_tokens, content)
