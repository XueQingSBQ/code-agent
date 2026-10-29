"""Tests for performance metrics collection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from carbide.execution.metrics import (
    ExecutionMetrics,
    MetricsCollector,
    PerformanceAnalyzer,
)


def test_metrics_collector_records_metrics():
    """Collector stores execution metrics."""
    collector = MetricsCollector()

    metric = ExecutionMetrics(
        tool_name="bash",
        duration_ms=100.0,
        timestamp=datetime.now(timezone.utc),
        success=True,
    )

    collector.record(metric)

    assert len(collector) == 1
    assert collector.metrics[0] == metric


def test_metrics_collector_summary():
    """Summary aggregates statistics correctly."""
    collector = MetricsCollector()

    # Record multiple metrics
    now = datetime.now(timezone.utc)
    collector.record(ExecutionMetrics("bash", 100.0, now, True))
    collector.record(ExecutionMetrics("bash", 200.0, now, True))
    collector.record(ExecutionMetrics("read", 50.0, now, True))
    collector.record(ExecutionMetrics("read", 150.0, now, False))

    summary = collector.get_summary()

    assert summary["total_executions"] == 4
    assert summary["total_duration_ms"] == 500.0
    assert summary["success_rate"] == 0.75

    # Check bash stats
    bash_stats = summary["by_tool"]["bash"]
    assert bash_stats["count"] == 2
    assert bash_stats["avg_ms"] == 150.0
    assert bash_stats["max_ms"] == 200.0
    assert bash_stats["min_ms"] == 100.0

    # Check read stats
    read_stats = summary["by_tool"]["read"]
    assert read_stats["count"] == 2
    assert read_stats["avg_ms"] == 100.0


def test_metrics_collector_empty_summary():
    """Summary handles empty metrics."""
    collector = MetricsCollector()

    summary = collector.get_summary()

    assert summary["total_executions"] == 0
    assert summary["total_duration_ms"] == 0.0
    assert summary["success_rate"] == 0.0
    assert summary["by_tool"] == {}


def test_metrics_collector_tool_stats():
    """Get statistics for specific tool."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    collector.record(ExecutionMetrics("bash", 100.0, now, True))
    collector.record(ExecutionMetrics("bash", 200.0, now, True))
    collector.record(ExecutionMetrics("bash", 300.0, now, False))

    stats = collector.get_tool_stats("bash")

    assert stats["count"] == 3
    assert stats["avg_ms"] == 200.0
    assert stats["max_ms"] == 300.0
    assert stats["min_ms"] == 100.0
    assert stats["success_rate"] == pytest.approx(2 / 3)


def test_metrics_collector_nonexistent_tool():
    """Tool stats for nonexistent tool returns zeros."""
    collector = MetricsCollector()

    stats = collector.get_tool_stats("nonexistent")

    assert stats["count"] == 0
    assert stats["avg_ms"] == 0.0
    assert stats["max_ms"] == 0.0
    assert stats["min_ms"] == 0.0
    assert stats["success_rate"] == 0.0


def test_metrics_collector_slowest_tools():
    """Get slowest tools by average duration."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    collector.record(ExecutionMetrics("fast", 10.0, now, True))
    collector.record(ExecutionMetrics("medium", 100.0, now, True))
    collector.record(ExecutionMetrics("slow", 1000.0, now, True))
    collector.record(ExecutionMetrics("slow", 2000.0, now, True))

    slowest = collector.get_slowest_tools(limit=2)

    assert len(slowest) == 2
    assert slowest[0][0] == "slow"
    assert slowest[0][1] == 1500.0  # Average of 1000 and 2000
    assert slowest[1][0] == "medium"
    assert slowest[1][1] == 100.0


def test_metrics_collector_error_summary():
    """Error summary counts errors by type."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    collector.record(ExecutionMetrics("bash", 100.0, now, False, error_type="TimeoutError"))
    collector.record(ExecutionMetrics("read", 50.0, now, False, error_type="FileNotFoundError"))
    collector.record(ExecutionMetrics("bash", 100.0, now, False, error_type="TimeoutError"))
    collector.record(ExecutionMetrics("write", 75.0, now, True))

    error_summary = collector.get_error_summary()

    assert error_summary["TimeoutError"] == 2
    assert error_summary["FileNotFoundError"] == 1
    assert "SuccessType" not in error_summary


def test_metrics_collector_clear():
    """Clear removes all metrics."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    collector.record(ExecutionMetrics("bash", 100.0, now, True))
    collector.record(ExecutionMetrics("read", 50.0, now, True))

    assert len(collector) == 2

    collector.clear()

    assert len(collector) == 0
    assert collector.get_summary()["total_executions"] == 0


def test_metrics_collector_get_metrics_since():
    """Get metrics since a specific timestamp."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)
    past = now - timedelta(hours=1)

    collector.record(ExecutionMetrics("bash", 100.0, past, True))
    collector.record(ExecutionMetrics("read", 50.0, now, True))

    cutoff = now - timedelta(minutes=30)
    recent_metrics = collector.get_metrics_since(cutoff)

    assert len(recent_metrics) == 1
    assert recent_metrics[0].tool_name == "read"


def test_performance_analyzer_identify_bottlenecks():
    """Analyzer identifies slow tools."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    collector.record(ExecutionMetrics("fast", 100.0, now, True))
    collector.record(ExecutionMetrics("slow", 1500.0, now, True))
    collector.record(ExecutionMetrics("slow", 2500.0, now, True))

    analyzer = PerformanceAnalyzer(collector)
    bottlenecks = analyzer.identify_bottlenecks(threshold_ms=1000.0)

    assert len(bottlenecks) == 1
    assert "slow" in bottlenecks[0]
    assert "2000.0ms" in bottlenecks[0]  # Average


def test_performance_analyzer_identify_unreliable_tools():
    """Analyzer identifies tools with low success rates."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    # Reliable tool (100% success)
    for _ in range(5):
        collector.record(ExecutionMetrics("reliable", 100.0, now, True))

    # Unreliable tool (40% success)
    for _ in range(3):
        collector.record(ExecutionMetrics("unreliable", 100.0, now, True))
    for _ in range(5):
        collector.record(ExecutionMetrics("unreliable", 100.0, now, False))

    analyzer = PerformanceAnalyzer(collector)
    unreliable = analyzer.identify_unreliable_tools(min_success_rate=0.9)

    assert len(unreliable) == 1
    assert "unreliable" in unreliable[0]
    assert "37.5%" in unreliable[0]  # 3/8 = 37.5%


def test_performance_analyzer_skips_low_count_tools():
    """Analyzer ignores tools with too few executions."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    # Only 3 executions (below threshold of 5)
    collector.record(ExecutionMetrics("rare", 100.0, now, False))
    collector.record(ExecutionMetrics("rare", 100.0, now, False))
    collector.record(ExecutionMetrics("rare", 100.0, now, False))

    analyzer = PerformanceAnalyzer(collector)
    unreliable = analyzer.identify_unreliable_tools(min_success_rate=0.9)

    # Should not be flagged (not enough data)
    assert len(unreliable) == 0


def test_performance_analyzer_generate_report():
    """Report generation produces readable output."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    collector.record(ExecutionMetrics("bash", 100.0, now, True))
    collector.record(ExecutionMetrics("read", 1500.0, now, True))
    collector.record(ExecutionMetrics("write", 50.0, now, False, error_type="PermissionError"))

    analyzer = PerformanceAnalyzer(collector)
    report = analyzer.generate_report()

    assert "Performance Report" in report
    assert "Total executions: 3" in report
    assert "Slowest Tools:" in report
    assert "read" in report  # Slowest tool
    assert "Errors:" in report
    assert "PermissionError" in report


def test_performance_analyzer_empty_report():
    """Report handles empty metrics gracefully."""
    collector = MetricsCollector()
    analyzer = PerformanceAnalyzer(collector)

    report = analyzer.generate_report()

    assert "No metrics collected" in report


def test_execution_metrics_with_token_count():
    """Metrics can include token count."""
    metric = ExecutionMetrics(
        tool_name="bash",
        duration_ms=100.0,
        timestamp=datetime.now(timezone.utc),
        success=True,
        token_count=1500,
    )

    assert metric.token_count == 1500


def test_metrics_collector_aggregates_multiple_tools():
    """Collector correctly handles many different tools."""
    collector = MetricsCollector()
    now = datetime.now(timezone.utc)

    tools = ["bash", "read", "write", "edit", "glob", "grep"]
    for tool in tools:
        collector.record(ExecutionMetrics(tool, 100.0, now, True))

    summary = collector.get_summary()

    assert len(summary["by_tool"]) == 6
    for tool in tools:
        assert tool in summary["by_tool"]


def test_metrics_collector_reports_percentiles_and_bounds_memory():
    collector = MetricsCollector(max_samples=3)
    now = datetime.now(timezone.utc)
    for duration in (10.0, 20.0, 30.0, 40.0):
        collector.record(ExecutionMetrics("read", duration, now, True))

    summary = collector.get_summary()
    assert summary["total_executions"] == 3
    assert summary["recorded_executions"] == 4
    assert summary["dropped_samples"] == 1
    assert summary["by_tool"]["read"]["p50_ms"] == 30.0
    assert summary["by_tool"]["read"]["p95_ms"] == pytest.approx(39.0)
    assert summary["by_tool"]["read"]["p99_ms"] == pytest.approx(39.8)


def test_metrics_collector_validates_limits_and_clear_resets_counters():
    with pytest.raises(ValueError, match="max_samples"):
        MetricsCollector(max_samples=0)
    with pytest.raises(ValueError, match="limit"):
        MetricsCollector().get_slowest_tools(-1)

    collector = MetricsCollector(max_samples=1)
    now = datetime.now(timezone.utc)
    collector.record(ExecutionMetrics("read", 1.0, now, True))
    collector.record(ExecutionMetrics("read", 2.0, now, True))
    collector.clear()
    assert collector.get_summary()["recorded_executions"] == 0
    assert collector.get_summary()["dropped_samples"] == 0
