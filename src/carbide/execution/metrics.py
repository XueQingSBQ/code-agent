"""Performance metrics collection for tool execution.

M3 execution layer: collects and analyzes execution metrics to identify
bottlenecks and optimize performance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import floor
from typing import Any


@dataclass
class ExecutionMetrics:
    """Metrics for a single tool execution."""

    tool_name: str
    duration_ms: float
    timestamp: datetime
    success: bool
    token_count: int = 0
    error_type: str | None = None


class MetricsCollector:
    """Collects and aggregates execution metrics."""

    def __init__(self, *, max_samples: int = 10_000) -> None:
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        self.metrics: list[ExecutionMetrics] = []
        self.max_samples = max_samples
        self.total_recorded = 0
        self.dropped_samples = 0

    def record(self, metric: ExecutionMetrics) -> None:
        """Record a single execution metric."""
        self.metrics.append(metric)
        self.total_recorded += 1
        if len(self.metrics) > self.max_samples:
            self.metrics.pop(0)
            self.dropped_samples += 1

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        """Calculate a linear-interpolated percentile for non-empty values."""
        ordered = sorted(values)
        position = (len(ordered) - 1) * percentile
        lower = floor(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    def get_summary(self) -> dict[str, Any]:
        """Generate summary statistics across all metrics."""
        if not self.metrics:
            return {
                "total_executions": 0,
                "total_duration_ms": 0.0,
                "success_rate": 0.0,
                "by_tool": {},
                "recorded_executions": self.total_recorded,
                "dropped_samples": self.dropped_samples,
            }

        by_tool: dict[str, list[float]] = defaultdict(list)
        successes = 0
        total_duration = 0.0

        for m in self.metrics:
            by_tool[m.tool_name].append(m.duration_ms)
            if m.success:
                successes += 1
            total_duration += m.duration_ms

        tool_stats = {}
        for tool_name, durations in by_tool.items():
            tool_stats[tool_name] = {
                "count": len(durations),
                "avg_ms": sum(durations) / len(durations),
                "max_ms": max(durations),
                "min_ms": min(durations),
                "total_ms": sum(durations),
                "p50_ms": self._percentile(durations, 0.50),
                "p95_ms": self._percentile(durations, 0.95),
                "p99_ms": self._percentile(durations, 0.99),
            }

        return {
            "total_executions": len(self.metrics),
            "total_duration_ms": total_duration,
            "success_rate": successes / len(self.metrics),
            "by_tool": tool_stats,
            "recorded_executions": self.total_recorded,
            "dropped_samples": self.dropped_samples,
        }

    def get_tool_stats(self, tool_name: str) -> dict[str, Any]:
        """Get statistics for a specific tool."""
        tool_metrics = [m for m in self.metrics if m.tool_name == tool_name]

        if not tool_metrics:
            return {
                "count": 0,
                "avg_ms": 0.0,
                "max_ms": 0.0,
                "min_ms": 0.0,
                "success_rate": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
            }

        durations = [m.duration_ms for m in tool_metrics]
        successes = sum(1 for m in tool_metrics if m.success)

        return {
            "count": len(tool_metrics),
            "avg_ms": sum(durations) / len(durations),
            "max_ms": max(durations),
            "min_ms": min(durations),
            "success_rate": successes / len(tool_metrics),
            "p50_ms": self._percentile(durations, 0.50),
            "p95_ms": self._percentile(durations, 0.95),
            "p99_ms": self._percentile(durations, 0.99),
        }

    def get_slowest_tools(self, limit: int = 5) -> list[tuple[str, float]]:
        """Get the slowest tools by average duration."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        summary = self.get_summary()
        tool_stats = summary["by_tool"]

        sorted_tools = sorted(
            tool_stats.items(),
            key=lambda x: x[1]["avg_ms"],
            reverse=True,
        )

        return [(name, stats["avg_ms"]) for name, stats in sorted_tools[:limit]]

    def get_error_summary(self) -> dict[str, int]:
        """Get count of errors by type."""
        error_counts: dict[str, int] = defaultdict(int)

        for m in self.metrics:
            if not m.success and m.error_type:
                error_counts[m.error_type] += 1

        return dict(error_counts)

    def clear(self) -> None:
        """Clear all collected metrics."""
        self.metrics.clear()
        self.total_recorded = 0
        self.dropped_samples = 0

    def get_metrics_since(self, timestamp: datetime) -> list[ExecutionMetrics]:
        """Get all metrics since a specific timestamp."""
        return [m for m in self.metrics if m.timestamp >= timestamp]

    def __len__(self) -> int:
        return len(self.metrics)


class PerformanceAnalyzer:
    """Analyzes performance metrics to identify issues."""

    def __init__(self, collector: MetricsCollector):
        self.collector = collector

    def identify_bottlenecks(self, threshold_ms: float = 1000.0) -> list[str]:
        """Identify tools that consistently take longer than threshold."""
        summary = self.collector.get_summary()
        bottlenecks = []

        for tool_name, stats in summary["by_tool"].items():
            if stats["avg_ms"] > threshold_ms:
                bottlenecks.append(
                    f"{tool_name}: avg {stats['avg_ms']:.1f}ms (max {stats['max_ms']:.1f}ms)"
                )

        return bottlenecks

    def identify_unreliable_tools(self, min_success_rate: float = 0.9) -> list[str]:
        """Identify tools with low success rates."""
        summary = self.collector.get_summary()
        unreliable = []

        for tool_name in summary["by_tool"]:
            stats = self.collector.get_tool_stats(tool_name)
            if stats["count"] >= 5 and stats["success_rate"] < min_success_rate:
                unreliable.append(
                    f"{tool_name}: {stats['success_rate']:.1%} success rate "
                    f"({stats['count']} executions)"
                )

        return unreliable

    def generate_report(self) -> str:
        """Generate a human-readable performance report."""
        summary = self.collector.get_summary()

        if summary["total_executions"] == 0:
            return "No metrics collected yet."

        lines = [
            "Performance Report",
            "=" * 60,
            f"Total executions: {summary['total_executions']}",
            f"Total duration: {summary['total_duration_ms']:.1f}ms",
            f"Overall success rate: {summary['success_rate']:.1%}",
            "",
            "Slowest Tools:",
            "-" * 60,
        ]

        for tool_name, avg_ms in self.collector.get_slowest_tools(5):
            lines.append(f"  {tool_name}: {avg_ms:.1f}ms avg")

        errors = self.collector.get_error_summary()
        if errors:
            lines.extend(["", "Errors:", "-" * 60])
            for error_type, count in errors.items():
                lines.append(f"  {error_type}: {count}")

        bottlenecks = self.identify_bottlenecks(1000.0)
        if bottlenecks:
            lines.extend(["", "Bottlenecks (>1000ms):", "-" * 60])
            for bottleneck in bottlenecks:
                lines.append(f"  {bottleneck}")

        unreliable = self.identify_unreliable_tools(0.9)
        if unreliable:
            lines.extend(["", "Unreliable Tools (<90% success):", "-" * 60])
            for tool in unreliable:
                lines.append(f"  {tool}")

        return "\n".join(lines)
