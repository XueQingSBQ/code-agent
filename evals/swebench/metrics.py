"""
Metrics tracking for SWE-bench evaluation.

Tracks:
- Token consumption per instance
- Cost per instance (based on model pricing)
- Time per instance
- Success/failure outcomes
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InstanceMetrics:
    """Metrics for a single SWE-bench instance evaluation."""

    instance_id: str
    start_time: datetime
    end_time: datetime | None = None

    # Outcome
    resolved: bool = False
    error: str | None = None

    # Token usage
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    # Performance
    num_turns: int = 0
    num_tool_calls: int = 0

    # Test results
    tests_passed: list[str] = field(default_factory=list)
    tests_failed: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """Total execution time in seconds."""
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time).total_seconds()

    @property
    def cost_usd(self) -> float:
        """
        Estimated cost in USD based on Anthropic pricing.

        Pricing (as of implementation):
        - Input: $3 / 1M tokens
        - Output: $15 / 1M tokens
        - Cached input: $0.30 / 1M tokens
        """
        input_cost = (self.prompt_tokens - self.cached_tokens) * 3 / 1_000_000
        cached_cost = self.cached_tokens * 0.30 / 1_000_000
        output_cost = self.completion_tokens * 15 / 1_000_000

        return input_cost + cached_cost + output_cost

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "instance_id": self.instance_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "resolved": self.resolved,
            "error": self.error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "num_turns": self.num_turns,
            "num_tool_calls": self.num_tool_calls,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "duration_seconds": self.duration_seconds,
            "cost_usd": self.cost_usd,
        }


@dataclass
class AggregateMetrics:
    """Aggregate metrics across all evaluated instances."""

    total_instances: int
    resolved_instances: int
    failed_instances: int
    error_instances: int

    total_tokens: int
    total_cost_usd: float
    total_duration_seconds: float

    avg_tokens_per_instance: float
    avg_cost_per_instance: float
    avg_duration_per_instance: float

    resolve_rate: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "total_instances": self.total_instances,
            "resolved_instances": self.resolved_instances,
            "failed_instances": self.failed_instances,
            "error_instances": self.error_instances,
            "resolve_rate": f"{self.resolve_rate:.1%}",
            "total_tokens": self.total_tokens,
            "total_cost_usd": f"${self.total_cost_usd:.2f}",
            "total_duration_seconds": self.total_duration_seconds,
            "avg_tokens_per_instance": int(self.avg_tokens_per_instance),
            "avg_cost_per_instance": f"${self.avg_cost_per_instance:.2f}",
            "avg_duration_per_instance": f"{self.avg_duration_per_instance:.1f}s",
        }


def compute_aggregate_metrics(instance_metrics: list[InstanceMetrics]) -> AggregateMetrics:
    """Compute aggregate metrics from individual instance metrics."""
    if not instance_metrics:
        return AggregateMetrics(
            total_instances=0,
            resolved_instances=0,
            failed_instances=0,
            error_instances=0,
            total_tokens=0,
            total_cost_usd=0.0,
            total_duration_seconds=0.0,
            avg_tokens_per_instance=0.0,
            avg_cost_per_instance=0.0,
            avg_duration_per_instance=0.0,
            resolve_rate=0.0,
        )

    total_instances = len(instance_metrics)
    resolved = sum(1 for m in instance_metrics if m.resolved)
    errored = sum(1 for m in instance_metrics if m.error is not None)
    failed = total_instances - resolved - errored

    total_tokens = sum(m.total_tokens for m in instance_metrics)
    total_cost = sum(m.cost_usd for m in instance_metrics)
    total_duration = sum(m.duration_seconds for m in instance_metrics)

    return AggregateMetrics(
        total_instances=total_instances,
        resolved_instances=resolved,
        failed_instances=failed,
        error_instances=errored,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        total_duration_seconds=total_duration,
        avg_tokens_per_instance=total_tokens / total_instances,
        avg_cost_per_instance=total_cost / total_instances,
        avg_duration_per_instance=total_duration / total_instances,
        resolve_rate=resolved / total_instances,
    )
