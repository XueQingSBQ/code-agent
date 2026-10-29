"""
Context efficiency A/B testing suite.

Runs the same task set with context pyramid enabled and disabled,
measuring token consumption and task completion rates.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carbide.sdk import Agent


@dataclass
class Task:
    """A single test task."""

    id: str
    description: str
    workspace: Path
    validation: str  # How to validate completion


# Task suite for context efficiency testing
TASK_SUITE = [
    Task(
        id="multi_file_refactor",
        description="Refactor the authentication module: extract validateToken into a separate function",
        workspace=Path("test_workspace/auth_app"),
        validation="grep -r 'function validateToken' src/",
    ),
    Task(
        id="bug_fix_with_context",
        description="Fix the null pointer exception in UserService.getUser when email is missing",
        workspace=Path("test_workspace/user_service"),
        validation="npm test -- UserService.test.js",
    ),
    Task(
        id="add_feature_multi_turn",
        description="Add rate limiting to the /api/login endpoint: 5 requests per minute per IP",
        workspace=Path("test_workspace/api_server"),
        validation="grep -r 'rate.*limit' src/ && npm test",
    ),
    Task(
        id="documentation_update",
        description="Update README.md with installation instructions for Docker deployment",
        workspace=Path("test_workspace/docs_project"),
        validation="grep -i 'docker' README.md",
    ),
    Task(
        id="test_coverage_improvement",
        description="Add unit tests for the PaymentProcessor class, covering success and failure cases",
        workspace=Path("test_workspace/payment_app"),
        validation="npm test -- PaymentProcessor.test.js",
    ),
]


@dataclass
class TaskResult:
    """Result of running a single task."""

    task_id: str
    success: bool
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    num_turns: int
    num_tool_calls: int
    duration_seconds: float
    error: str | None = None


@dataclass
class ABTestResult:
    """Result of A/B test comparing pyramid on vs off."""

    pyramid_enabled_results: list[TaskResult]
    pyramid_disabled_results: list[TaskResult]

    @property
    def token_reduction_percent(self) -> float:
        """Calculate token reduction percentage."""
        enabled_total = sum(r.total_tokens for r in self.pyramid_enabled_results)
        disabled_total = sum(r.total_tokens for r in self.pyramid_disabled_results)

        if disabled_total == 0:
            return 0.0

        reduction = (disabled_total - enabled_total) / disabled_total * 100
        return reduction

    @property
    def completion_rate_enabled(self) -> float:
        """Task completion rate with pyramid enabled."""
        if not self.pyramid_enabled_results:
            return 0.0
        success_count = sum(1 for r in self.pyramid_enabled_results if r.success)
        return success_count / len(self.pyramid_enabled_results)

    @property
    def completion_rate_disabled(self) -> float:
        """Task completion rate with pyramid disabled."""
        if not self.pyramid_disabled_results:
            return 0.0
        success_count = sum(1 for r in self.pyramid_disabled_results if r.success)
        return success_count / len(self.pyramid_disabled_results)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "token_reduction_percent": f"{self.token_reduction_percent:.1f}%",
            "completion_rate_pyramid_on": f"{self.completion_rate_enabled:.1%}",
            "completion_rate_pyramid_off": f"{self.completion_rate_disabled:.1%}",
            "pyramid_enabled": {
                "total_tokens": sum(r.total_tokens for r in self.pyramid_enabled_results),
                "avg_tokens_per_task": (
                    sum(r.total_tokens for r in self.pyramid_enabled_results)
                    / len(self.pyramid_enabled_results)
                    if self.pyramid_enabled_results
                    else 0
                ),
                "tasks": [
                    {
                        "id": r.task_id,
                        "success": r.success,
                        "tokens": r.total_tokens,
                        "turns": r.num_turns,
                    }
                    for r in self.pyramid_enabled_results
                ],
            },
            "pyramid_disabled": {
                "total_tokens": sum(r.total_tokens for r in self.pyramid_disabled_results),
                "avg_tokens_per_task": (
                    sum(r.total_tokens for r in self.pyramid_disabled_results)
                    / len(self.pyramid_disabled_results)
                    if self.pyramid_disabled_results
                    else 0
                ),
                "tasks": [
                    {
                        "id": r.task_id,
                        "success": r.success,
                        "tokens": r.total_tokens,
                        "turns": r.num_turns,
                    }
                    for r in self.pyramid_disabled_results
                ],
            },
        }


async def run_task(
    task: Task, enable_pyramid: bool, api_key: str, model: str
) -> TaskResult:
    """
    Run a single task and collect metrics.

    Args:
        task: The task to run
        enable_pyramid: Whether to enable context pyramid
        api_key: API key
        model: Model to use

    Returns:
        TaskResult with metrics
    """
    import asyncio
    from datetime import datetime

    start_time = datetime.now()

    # Create agent with or without pyramid
    agent = Agent(
        api_key=api_key,
        model=model,
        workspace=task.workspace,
        enable_context_pyramid=enable_pyramid,
    )

    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    num_turns = 0
    num_tool_calls = 0
    error = None
    success = False

    try:
        async with agent.query(task.description) as events:
            async for event in events:
                if event.type == "model_response":
                    num_turns += 1
                    if hasattr(event, "usage"):
                        prompt_tokens += event.usage.input_tokens
                        completion_tokens += event.usage.output_tokens
                        total_tokens += event.usage.input_tokens + event.usage.output_tokens
                        cached_tokens += getattr(event.usage, "cache_read_input_tokens", 0)

                elif event.type == "tool_result":
                    num_tool_calls += 1

        # Validate completion
        import subprocess

        result = subprocess.run(
            task.validation,
            shell=True,
            cwd=task.workspace,
            capture_output=True,
            timeout=30,
        )
        success = result.returncode == 0

    except Exception as e:
        error = str(e)
        success = False

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    return TaskResult(
        task_id=task.id,
        success=success,
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        num_turns=num_turns,
        num_tool_calls=num_tool_calls,
        duration_seconds=duration,
        error=error,
    )


async def run_ab_test(
    tasks: list[Task],
    api_key: str,
    model: str,
    output_path: Path,
) -> ABTestResult:
    """
    Run A/B test: same tasks with pyramid on vs off.

    Args:
        tasks: List of tasks to run
        api_key: API key
        model: Model to use
        output_path: Where to save results

    Returns:
        ABTestResult with comparison metrics
    """
    import json

    print("=" * 60)
    print("CONTEXT EFFICIENCY A/B TEST")
    print("=" * 60)
    print(f"Tasks: {len(tasks)}")
    print(f"Model: {model}")
    print()

    # Run with pyramid enabled
    print("Phase 1: Running with context pyramid ENABLED")
    print("-" * 60)
    enabled_results = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] Running: {task.id}")
        result = await run_task(task, enable_pyramid=True, api_key=api_key, model=model)
        enabled_results.append(result)
        status = "✓" if result.success else "✗"
        print(f"  {status} {result.total_tokens:,} tokens, {result.num_turns} turns")

    print()

    # Run with pyramid disabled
    print("Phase 2: Running with context pyramid DISABLED")
    print("-" * 60)
    disabled_results = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] Running: {task.id}")
        result = await run_task(task, enable_pyramid=False, api_key=api_key, model=model)
        disabled_results.append(result)
        status = "✓" if result.success else "✗"
        print(f"  {status} {result.total_tokens:,} tokens, {result.num_turns} turns")

    print()

    # Generate comparison
    ab_result = ABTestResult(
        pyramid_enabled_results=enabled_results,
        pyramid_disabled_results=disabled_results,
    )

    # Save results
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(ab_result.to_dict(), f, indent=2)

    # Print summary
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Token reduction: {ab_result.token_reduction_percent:.1f}%")
    print(f"Completion rate (pyramid ON):  {ab_result.completion_rate_enabled:.1%}")
    print(f"Completion rate (pyramid OFF): {ab_result.completion_rate_disabled:.1%}")
    print()
    print(f"Target: ≥40% token reduction, completion rate unchanged")
    print(f"Result saved to: {output_path}")

    return ab_result
