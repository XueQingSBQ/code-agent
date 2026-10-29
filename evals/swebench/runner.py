"""
Main evaluation runner for SWE-bench Lite.

Runs Carbide against SWE-bench Lite instances and collects metrics.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from carbide.sdk import Agent

from .docker import DockerBashTool, DockerEnvironment, DockerResourceLimits
from .instance import SWEBenchInstance, format_task_prompt, load_instances
from .metrics import AggregateMetrics, InstanceMetrics, compute_aggregate_metrics


async def evaluate_instance(
    instance: SWEBenchInstance,
    work_dir: Path,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout_seconds: int = 1800,
    docker_resources: DockerResourceLimits | None = None,
) -> InstanceMetrics:
    """
    Evaluate Carbide on a single SWE-bench instance.

    Args:
        instance: The problem instance
        work_dir: Working directory for this evaluation
        api_key: Anthropic API key
        model: Model to use
        base_url: Optional custom API endpoint
        timeout_seconds: Maximum time per instance
        docker_resources: Optional CPU, memory, and PID ceilings

    Returns:
        Metrics for this evaluation
    """
    metrics = InstanceMetrics(
        instance_id=instance.instance_id,
        start_time=datetime.now(UTC),
    )

    if docker_resources is None:
        docker_env = DockerEnvironment(instance, work_dir)
    else:
        docker_env = DockerEnvironment(instance, work_dir, docker_resources)

    try:
        # Set up Docker environment
        await docker_env.setup()

        # Prepare task prompt
        task_prompt = format_task_prompt(instance)

        # Run Carbide agent
        from carbide.tools.defaults import create_default_registry

        registry = create_default_registry()
        registry.replace(DockerBashTool(docker_env))
        agent = Agent(
            api_key=api_key,
            model=model,
            base_url=base_url,
            project_dir=work_dir / "repo",
            tool_registry=registry,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            max_turns=50,
            # Evaluation happens inside a disposable container, so the
            # evaluator must not stall on an interactive approval prompt.
            approval_callback=lambda _call, _prompt: True,
        )

        async with asyncio.timeout(timeout_seconds):
            async with agent:
                async for event in agent.query(
                    task_prompt,
                    tools=registry.api_schemas(),
                ):
                    if event.type == "turn_complete" and isinstance(event.data, dict):
                        metrics.num_turns += 1
                        input_tokens = int(event.data.get("input_tokens", 0))
                        output_tokens = int(event.data.get("output_tokens", 0))
                        metrics.prompt_tokens += input_tokens
                        metrics.completion_tokens += output_tokens
                        metrics.total_tokens += input_tokens + output_tokens
                        metrics.cached_tokens += int(event.data.get("cache_read_tokens", 0))
                        reason = event.data.get("reason")
                        if reason and reason != "completed":
                            metrics.error = f"Agent terminated with reason: {reason}"
                    elif event.type == "tool_complete":
                        metrics.num_tool_calls += 1

        # A failed/limited agent run is not a resolved instance and should not
        # be masked by a later test harness result.
        if metrics.error is not None:
            return metrics

        # Apply test patch and run tests.
        await docker_env.apply_test_patch()
        test_results = await docker_env.run_tests()

        # Check if resolved: all FAIL_TO_PASS tests should now pass
        fail_to_pass_passing = all(
            test in test_results["passed_tests"] for test in instance.FAIL_TO_PASS
        )
        pass_to_pass_passing = all(
            test in test_results["passed_tests"] for test in instance.PASS_TO_PASS
        )

        metrics.resolved = fail_to_pass_passing and pass_to_pass_passing
        metrics.tests_passed = test_results["passed_tests"]
        metrics.tests_failed = test_results["failed_tests"]

    except TimeoutError:
        metrics.error = f"Timeout after {timeout_seconds}s"
    except Exception as e:
        metrics.error = str(e)
    finally:
        metrics.end_time = datetime.now(UTC)
        await docker_env.cleanup()

    return metrics


async def run_evaluation(
    dataset_path: Path,
    output_dir: Path,
    api_key: str,
    model: str,
    base_url: str | None = None,
    max_instances: int | None = None,
    parallel: int = 1,
    timeout_seconds: int = 1800,
    docker_resources: DockerResourceLimits | None = None,
) -> AggregateMetrics:
    """
    Run SWE-bench Lite evaluation.

    Args:
        dataset_path: Path to SWE-bench Lite dataset JSON
        output_dir: Directory for outputs and logs
        api_key: Anthropic API key
        model: Model to use
        base_url: Optional custom API endpoint
        max_instances: Limit number of instances (for testing)
        parallel: Number of parallel evaluations
        timeout_seconds: Maximum time per instance
        docker_resources: Optional CPU, memory, and PID ceilings

    Returns:
        Aggregate metrics across all instances
    """
    if parallel < 1:
        raise ValueError("parallel must be at least 1")
    if max_instances is not None and max_instances < 0:
        raise ValueError("max_instances must be non-negative")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be at least 1")

    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)

    # Load instances
    instances = load_instances(dataset_path)
    if max_instances is not None:
        instances = instances[:max_instances]

    print(f"Evaluating {len(instances)} instances with {parallel} parallel workers")

    # Run evaluations
    all_metrics: list[InstanceMetrics] = []

    # Create semaphore to limit parallelism
    sem = asyncio.Semaphore(parallel)

    async def evaluate_with_limit(inst: SWEBenchInstance, idx: int) -> InstanceMetrics:
        async with sem:
            print(f"[{idx + 1}/{len(instances)}] Starting {inst.instance_id}")
            work_dir = output_dir / inst.instance_id.replace("/", "_")
            await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)

            if docker_resources is None:
                metrics = await evaluate_instance(
                    inst,
                    work_dir,
                    api_key,
                    model,
                    base_url,
                    timeout_seconds,
                )
            else:
                metrics = await evaluate_instance(
                    inst,
                    work_dir,
                    api_key,
                    model,
                    base_url,
                    timeout_seconds,
                    docker_resources,
                )

            # Save individual metrics
            metrics_file = output_dir / f"{inst.instance_id.replace('/', '_')}.json"
            await asyncio.to_thread(
                metrics_file.write_text,
                json.dumps(metrics.to_dict(), indent=2),
                encoding="utf-8",
            )

            status = (
                "[OK] RESOLVED"
                if metrics.resolved
                else ("[ERR] ERROR" if metrics.error else "[FAIL] FAILED")
            )
            try:
                print(
                    f"[{idx + 1}/{len(instances)}] {inst.instance_id}: {status} "
                    f"({metrics.num_turns} turns, ${metrics.cost_usd:.2f})"
                )
            except UnicodeEncodeError:
                # Fallback for Windows console encoding issues
                print(
                    f"[{idx + 1}/{len(instances)}] {inst.instance_id}: {status} "
                    f"({metrics.num_turns} turns, ${metrics.cost_usd:.2f})".encode(
                        "ascii", "replace"
                    ).decode("ascii")
                )

            return metrics

    tasks = [evaluate_with_limit(inst, idx) for idx, inst in enumerate(instances)]
    all_metrics = await asyncio.gather(*tasks)

    # Compute aggregate metrics
    aggregate = compute_aggregate_metrics(all_metrics)

    # Save aggregate report
    report_file = output_dir / "aggregate_report.json"
    await asyncio.to_thread(
        report_file.write_text,
        json.dumps(
            {
                "aggregate": aggregate.to_dict(),
                "instances": [m.to_dict() for m in all_metrics],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n" + "=" * 60)
    print("EVALUATION COMPLETE")
    print("=" * 60)
    print(f"Resolve rate: {aggregate.resolve_rate:.1%}")
    print(f"Total cost: ${aggregate.total_cost_usd:.2f}")
    print(f"Avg cost/instance: ${aggregate.avg_cost_per_instance:.2f}")
    print(f"Avg tokens/instance: {aggregate.avg_tokens_per_instance:.0f}")
    print(f"Report saved to: {report_file}")

    return aggregate
