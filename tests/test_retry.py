"""Tests for retry policy."""

from __future__ import annotations

import asyncio
import time

import pytest

from carbide.execution.retry import (
    AGGRESSIVE_RETRY,
    CONSERVATIVE_RETRY,
    DEFAULT_RETRY,
    NO_RETRY,
    RateLimitError,
    RetryableOperation,
    RetryPolicy,
    TransientNetworkError,
)


def test_retry_policy_should_retry():
    """Policy determines if error should be retried."""
    policy = RetryPolicy(max_retries=3, retryable_errors={TimeoutError})

    # Retryable error, attempt 0
    assert policy.should_retry(TimeoutError(), 0)

    # Retryable error, attempt 2
    assert policy.should_retry(TimeoutError(), 2)

    # Max retries reached
    assert not policy.should_retry(TimeoutError(), 3)

    # Non-retryable error
    assert not policy.should_retry(ValueError(), 0)

    # Subclasses of a configured error are retryable too.
    assert RetryPolicy(max_retries=1).should_retry(ConnectionResetError(), 0)


def test_retry_policy_validates_bounds():
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        RetryPolicy(initial_delay=-0.1)


def test_retry_policy_exponential_backoff():
    """Backoff delay increases exponentially."""
    policy = RetryPolicy(
        initial_delay=1.0,
        backoff_factor=2.0,
        max_delay=100.0,
    )

    # Attempt 0: 1.0 * 2^0 = 1.0
    assert policy.get_delay(0) == 1.0

    # Attempt 1: 1.0 * 2^1 = 2.0
    assert policy.get_delay(1) == 2.0

    # Attempt 2: 1.0 * 2^2 = 4.0
    assert policy.get_delay(2) == 4.0

    # Attempt 3: 1.0 * 2^3 = 8.0
    assert policy.get_delay(3) == 8.0


def test_retry_policy_respects_max_delay():
    """Backoff delay capped at max_delay."""
    policy = RetryPolicy(
        initial_delay=10.0,
        backoff_factor=2.0,
        max_delay=30.0,
    )

    # Attempt 0: 10.0
    assert policy.get_delay(0) == 10.0

    # Attempt 1: 20.0
    assert policy.get_delay(1) == 20.0

    # Attempt 2: would be 40.0, capped at 30.0
    assert policy.get_delay(2) == 30.0

    # Attempt 3: would be 80.0, capped at 30.0
    assert policy.get_delay(3) == 30.0


@pytest.mark.asyncio
async def test_retryable_operation_success_on_first_try():
    """Operation succeeds on first attempt."""
    policy = RetryPolicy(max_retries=3)
    operation = RetryableOperation(policy)

    call_count = 0

    async def successful_operation():
        nonlocal call_count
        call_count += 1
        return "success"

    result = await operation.execute(successful_operation)

    assert result == "success"
    assert call_count == 1


@pytest.mark.asyncio
async def test_retryable_operation_success_after_retry():
    """Operation succeeds after retries."""
    policy = RetryPolicy(
        max_retries=3,
        initial_delay=0.01,  # Fast retry for testing
        retryable_errors={TimeoutError},
    )
    operation = RetryableOperation(policy)

    call_count = 0

    async def flaky_operation():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise TimeoutError("temporary failure")
        return "success"

    result = await operation.execute(flaky_operation)

    assert result == "success"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retryable_operation_all_retries_exhausted():
    """All retries exhausted, raises last error."""
    policy = RetryPolicy(
        max_retries=2,
        initial_delay=0.01,
        retryable_errors={TimeoutError},
    )
    operation = RetryableOperation(policy)

    call_count = 0

    async def always_failing_operation():
        nonlocal call_count
        call_count += 1
        raise TimeoutError("always fails")

    with pytest.raises(TimeoutError):
        await operation.execute(always_failing_operation)

    # Initial attempt + 2 retries = 3 calls
    assert call_count == 3


@pytest.mark.asyncio
async def test_retryable_operation_non_retryable_error():
    """Non-retryable error raised immediately."""
    policy = RetryPolicy(
        max_retries=3,
        retryable_errors={TimeoutError},
    )
    operation = RetryableOperation(policy)

    call_count = 0

    async def operation_with_non_retryable_error():
        nonlocal call_count
        call_count += 1
        raise ValueError("non-retryable")

    with pytest.raises(ValueError):
        await operation.execute(operation_with_non_retryable_error)

    # Should fail immediately without retries
    assert call_count == 1


@pytest.mark.asyncio
async def test_retryable_operation_on_retry_callback():
    """on_retry callback is called on each retry."""
    policy = RetryPolicy(
        max_retries=2,
        initial_delay=0.01,
        retryable_errors={TimeoutError},
    )
    operation = RetryableOperation(policy)

    retry_calls = []

    async def failing_operation():
        raise TimeoutError("fail")

    def on_retry(error, attempt):
        retry_calls.append((str(error), attempt))

    with pytest.raises(TimeoutError):
        await operation.execute(failing_operation, on_retry=on_retry)

    # Should have 2 retry callbacks (attempt 0 and 1)
    assert len(retry_calls) == 2
    assert retry_calls[0][1] == 0
    assert retry_calls[1][1] == 1


@pytest.mark.asyncio
async def test_retry_delay_timing():
    """Retry delays are applied correctly."""
    policy = RetryPolicy(
        max_retries=2,
        initial_delay=0.05,
        backoff_factor=2.0,
        retryable_errors={TimeoutError},
    )
    operation = RetryableOperation(policy)

    call_times = []

    async def failing_operation():
        call_times.append(time.perf_counter())
        raise TimeoutError("fail")

    start_time = time.perf_counter()

    with pytest.raises(TimeoutError):
        await operation.execute(failing_operation)

    # Check delays between calls
    # First retry: ~0.05s delay
    # Second retry: ~0.1s delay
    # Total: ~0.15s minimum

    total_time = time.perf_counter() - start_time
    assert total_time >= 0.14  # Allow some variance


def test_predefined_policies_exist():
    """Predefined retry policies are available."""
    assert DEFAULT_RETRY.max_retries == 3
    assert AGGRESSIVE_RETRY.max_retries == 5
    assert CONSERVATIVE_RETRY.max_retries == 2
    assert NO_RETRY.max_retries == 0


@pytest.mark.asyncio
async def test_custom_retryable_errors():
    """Policy can use custom retryable error types."""
    policy = RetryPolicy(
        max_retries=2,
        initial_delay=0.01,
        retryable_errors={TransientNetworkError, RateLimitError},
    )
    operation = RetryableOperation(policy)

    # TransientNetworkError should be retried
    call_count = 0

    async def transient_error_operation():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise TransientNetworkError("network issue")
        return "success"

    result = await operation.execute(transient_error_operation)
    assert result == "success"
    assert call_count == 2


@pytest.mark.asyncio
async def test_no_retry_policy():
    """NO_RETRY policy fails immediately."""
    operation = RetryableOperation(NO_RETRY)

    call_count = 0

    async def failing_operation():
        nonlocal call_count
        call_count += 1
        raise TimeoutError("fail")

    with pytest.raises(TimeoutError):
        await operation.execute(failing_operation)

    # Should only be called once
    assert call_count == 1
