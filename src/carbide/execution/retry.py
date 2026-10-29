"""Retry policy for tool execution with exponential backoff.

M3 execution layer: handles transient failures with configurable retry
strategies and exponential backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """Base class for errors that should trigger retry."""


class TransientNetworkError(RetryableError):
    """Network error that may be transient."""


class RateLimitError(RetryableError):
    """API rate limit exceeded."""


class ServiceUnavailableError(RetryableError):
    """Service temporarily unavailable."""


class RetryPolicy:
    """Policy for retrying failed operations."""

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        retryable_errors: set[type[Exception]] | None = None,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_factor < 0 or initial_delay < 0 or max_delay < 0:
            raise ValueError("retry delays and backoff_factor must be non-negative")
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.retryable_errors = (
            retryable_errors
            if retryable_errors is not None
            else {
                TimeoutError,
                ConnectionError,
                asyncio.TimeoutError,
                RetryableError,
            }
        )

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """Check if an error should trigger a retry."""
        if attempt < 0:
            raise ValueError("attempt must be non-negative")
        if attempt >= self.max_retries:
            return False

        # Match subclasses too: ConnectionResetError is a ConnectionError,
        # and custom transient errors normally inherit RetryableError.
        return isinstance(error, tuple(self.retryable_errors))

    def get_delay(self, attempt: int) -> float:
        """Calculate delay before next retry using exponential backoff."""
        delay = self.initial_delay * (self.backoff_factor**attempt)
        return min(delay, self.max_delay)


class RetryableOperation:
    """Wraps an operation with retry logic."""

    def __init__(self, policy: RetryPolicy):
        self.policy = policy

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        on_retry: Callable[[Exception, int], None] | None = None,
    ) -> T:
        """
        Execute an operation with retries.

        Args:
            operation: Async function to execute
            on_retry: Optional callback called on each retry attempt

        Returns:
            Result of the operation

        Raises:
            Last exception if all retries fail
        """
        last_error: Exception | None = None
        attempt = 0

        while attempt <= self.policy.max_retries:
            try:
                return await operation()
            except Exception as e:
                last_error = e

                if not self.policy.should_retry(e, attempt):
                    raise

                # Calculate delay and wait
                if attempt < self.policy.max_retries:
                    delay = self.policy.get_delay(attempt)

                    if on_retry:
                        on_retry(e, attempt)

                    await asyncio.sleep(delay)

                attempt += 1

        # All retries exhausted
        if last_error:
            raise last_error
        else:
            raise RuntimeError("Operation failed without exception")


# Predefined retry policies

DEFAULT_RETRY = RetryPolicy(
    max_retries=3,
    backoff_factor=2.0,
    initial_delay=1.0,
)

AGGRESSIVE_RETRY = RetryPolicy(
    max_retries=5,
    backoff_factor=2.0,
    initial_delay=0.5,
)

CONSERVATIVE_RETRY = RetryPolicy(
    max_retries=2,
    backoff_factor=3.0,
    initial_delay=2.0,
)

NO_RETRY = RetryPolicy(max_retries=0)
