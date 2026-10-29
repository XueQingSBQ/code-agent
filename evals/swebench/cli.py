#!/usr/bin/env python3
"""
CLI entry point for running SWE-bench Lite evaluation.

Usage:
    python -m evals.swebench.cli \
        --dataset path/to/swebench_lite.json \
        --output ./eval_results \
        --api-key $ANTHROPIC_API_KEY \
        --model claude-sonnet-4-20250514 \
        --max-instances 10

For custom API endpoints:
    python -m evals.swebench.cli \
        --dataset path/to/swebench_lite.json \
        --output ./eval_results \
        --api-key $ANTHROPIC_API_KEY \
        --base-url https://synai996.space \
        --model claude-sonnet-4-6 \
        --max-instances 10
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Run SWE-bench Lite evaluation with Carbide")
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to SWE-bench Lite dataset JSON file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory for results and logs",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        help="Custom API base URL (for compatible endpoints)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="claude-sonnet-4-20250514",
        help="Model to use (default: claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--max-instances",
        type=int,
        help="Limit number of instances to evaluate (for testing)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of parallel evaluations (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout per instance in seconds (default: 1800)",
    )
    parser.add_argument(
        "--docker-cpus",
        type=float,
        default=2.0,
        help="CPU limit per evaluation container (default: 2.0)",
    )
    parser.add_argument(
        "--docker-memory",
        type=str,
        default="4g",
        help="Memory limit per evaluation container (default: 4g)",
    )
    parser.add_argument(
        "--docker-pids-limit",
        type=int,
        default=512,
        help="PID limit per evaluation container (default: 512)",
    )

    args = parser.parse_args()

    if args.parallel < 1:
        parser.error("--parallel must be at least 1")
    if args.max_instances is not None and args.max_instances < 0:
        parser.error("--max-instances must be non-negative")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")

    # Get API key
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: API key required. Set --api-key or ANTHROPIC_API_KEY env var")
        sys.exit(1)

    # Validate dataset exists
    if not args.dataset.exists():
        print(f"Error: Dataset file not found: {args.dataset}")
        sys.exit(1)

    # Import here to avoid circular dependencies
    from .docker import DockerResourceLimits
    from .runner import run_evaluation

    try:
        docker_resources = DockerResourceLimits(
            cpus=args.docker_cpus,
            memory=args.docker_memory,
            pids_limit=args.docker_pids_limit,
        )
    except ValueError as exc:
        parser.error(str(exc))

    # Run evaluation
    print("Starting SWE-bench Lite evaluation")
    print(f"Dataset: {args.dataset}")
    print(f"Model: {args.model}")
    if args.base_url:
        print(f"Base URL: {args.base_url}")
    if args.max_instances:
        print(f"Max instances: {args.max_instances}")
    print(f"Output: {args.output}")
    print("-" * 60)

    asyncio.run(
        run_evaluation(
            dataset_path=args.dataset,
            output_dir=args.output,
            api_key=api_key,
            model=args.model,
            base_url=args.base_url,
            max_instances=args.max_instances,
            parallel=args.parallel,
            timeout_seconds=args.timeout,
            docker_resources=docker_resources,
        )
    )


if __name__ == "__main__":
    main()
