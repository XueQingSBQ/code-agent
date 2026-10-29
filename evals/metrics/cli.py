#!/usr/bin/env python3
"""
CLI for running M3 metric validation tests.

Usage:
    # Context efficiency (A/B test)
    python -m evals.metrics.cli context-efficiency \
        --api-key $ANTHROPIC_API_KEY \
        --model claude-sonnet-4-20250514

    # Fault tolerance (chaos test)
    python -m evals.metrics.cli fault-tolerance \
        --api-key $ANTHROPIC_API_KEY \
        --model claude-sonnet-4-20250514 \
        --repetitions 20

    # Both
    python -m evals.metrics.cli all \
        --api-key $ANTHROPIC_API_KEY
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Run M3 metric validation tests")

    subparsers = parser.add_subparsers(dest="command", help="Test to run")

    # Context efficiency command
    context_parser = subparsers.add_parser(
        "context-efficiency", help="Run context efficiency A/B test"
    )
    context_parser.add_argument("--api-key", type=str, help="API key")
    context_parser.add_argument(
        "--model", type=str, default="claude-sonnet-4-20250514", help="Model to use"
    )
    context_parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval_results/context_efficiency.json"),
        help="Output file",
    )

    # Fault tolerance command
    chaos_parser = subparsers.add_parser(
        "fault-tolerance", help="Run fault tolerance chaos tests"
    )
    chaos_parser.add_argument("--api-key", type=str, help="API key")
    chaos_parser.add_argument(
        "--model", type=str, default="claude-sonnet-4-20250514", help="Model to use"
    )
    chaos_parser.add_argument(
        "--repetitions", type=int, default=20, help="Repetitions per scenario"
    )
    chaos_parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval_results/fault_tolerance.json"),
        help="Output file",
    )

    # All command
    all_parser = subparsers.add_parser("all", help="Run all metric validation tests")
    all_parser.add_argument("--api-key", type=str, help="API key")
    all_parser.add_argument(
        "--model", type=str, default="claude-sonnet-4-20250514", help="Model to use"
    )
    all_parser.add_argument(
        "--output-dir", type=Path, default=Path("eval_results"), help="Output directory"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Get API key
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: API key required. Set --api-key or ANTHROPIC_API_KEY env var")
        sys.exit(1)

    # Run appropriate test(s)
    if args.command == "context-efficiency":
        run_context_efficiency(api_key, args.model, args.output)

    elif args.command == "fault-tolerance":
        run_fault_tolerance(api_key, args.model, args.repetitions, args.output)

    elif args.command == "all":
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        print("Running all M3 metric validation tests")
        print("=" * 60)
        print()

        # Run context efficiency
        print("1/2: Context Efficiency A/B Test")
        print("-" * 60)
        run_context_efficiency(api_key, args.model, output_dir / "context_efficiency.json")
        print()

        # Run fault tolerance
        print("2/2: Fault Tolerance Chaos Tests")
        print("-" * 60)
        run_fault_tolerance(
            api_key, args.model, 20, output_dir / "fault_tolerance.json"
        )
        print()

        print("=" * 60)
        print("ALL TESTS COMPLETE")
        print(f"Results saved to: {output_dir}")


def run_context_efficiency(api_key: str, model: str, output: Path) -> None:
    """Run context efficiency A/B test."""
    from evals.context_efficiency.ab_test import TASK_SUITE, run_ab_test

    result = asyncio.run(
        run_ab_test(
            tasks=TASK_SUITE,
            api_key=api_key,
            model=model,
            output_path=output,
        )
    )

    # Check if target met
    if result.token_reduction_percent >= 40.0:
        print(f"✓ TARGET MET: {result.token_reduction_percent:.1f}% ≥ 40%")
    else:
        print(f"✗ TARGET MISSED: {result.token_reduction_percent:.1f}% < 40%")


def run_fault_tolerance(
    api_key: str, model: str, repetitions: int, output: Path
) -> None:
    """Run fault tolerance chaos tests."""
    from evals.chaos.runner import CHAOS_SCENARIOS, run_chaos_suite

    report = asyncio.run(
        run_chaos_suite(
            scenarios=CHAOS_SCENARIOS,
            repetitions=repetitions,
            api_key=api_key,
            model=model,
            output_path=output,
        )
    )

    # Check if target met
    recovery_met = report.recovery_rate >= 0.95
    history_met = report.history_violation_count == 0

    if recovery_met and history_met:
        print(f"✓ TARGET MET: {report.recovery_rate:.1%} ≥ 95%, 0 violations")
    else:
        if not recovery_met:
            print(f"✗ Recovery rate: {report.recovery_rate:.1%} < 95%")
        if not history_met:
            print(f"✗ History violations: {report.history_violation_count} > 0")


if __name__ == "__main__":
    main()
