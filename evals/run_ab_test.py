"""
Simple A/B test runner for context efficiency validation.

Runs the same task with compression enabled/disabled and compares token usage.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from carbide.providers.anthropic import AnthropicProvider
from carbide.sdk import Agent


# Simple task set for quick validation
TASKS = [
    {
        "id": "refactor_auth",
        "prompt": """Look at the auth.js file in test_workspace/auth_app/src/.
Extract the validateToken logic into a separate function called validateToken().
Make the necessary changes.""",
        "workspace": Path("test_workspace/auth_app"),
    },
    {
        "id": "fix_user_service",
        "prompt": """Look at UserService.js in test_workspace/user_service/src/.
Fix the null pointer exception when email is missing in getUser method.
Add proper null checking.""",
        "workspace": Path("test_workspace/user_service"),
    },
    {
        "id": "update_readme",
        "prompt": """Look at README.md in test_workspace/docs_project/.
Add a new section called 'Docker Deployment' with basic instructions.
Include docker build and docker run commands.""",
        "workspace": Path("test_workspace/docs_project"),
    },
]


async def run_task_with_compression(task, api_key, base_url, model, enable_compression):
    """Run a single task and track token usage."""

    # Create provider
    provider = AnthropicProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    # Create agent
    agent = Agent(
        provider=provider,
        project_dir=task["workspace"],
        enable_compression=enable_compression,
    )

    # Track metrics
    start_time = time.time()
    response_text = []

    try:
        async for event in agent.query(task["prompt"]):
            if event.type == "chunk" and event.data:
                if hasattr(event.data, 'type') and event.data.type == "content_block_delta":
                    if hasattr(event.data, 'content') and event.data.content:
                        response_text.append(event.data.content)

        elapsed = time.time() - start_time

        # Get stats from agent
        stats = agent.get_stats()

        return {
            "task_id": task["id"],
            "success": True,
            "tokens": stats.get("total_tokens", 0),
            "turns": stats.get("turn_count", 0),
            "elapsed": elapsed,
            "response_length": len("".join(response_text)),
        }

    except Exception as e:
        return {
            "task_id": task["id"],
            "success": False,
            "error": str(e),
            "tokens": 0,
            "turns": 0,
            "elapsed": time.time() - start_time,
        }


async def run_ab_test(api_key, base_url, model, output_dir):
    """Run A/B test comparing compression on vs off."""

    print("=" * 60)
    print("CONTEXT EFFICIENCY A/B TEST")
    print("=" * 60)
    print(f"Model: {model}")
    print(f"Tasks: {len(TASKS)}")
    print(f"Output: {output_dir}")
    print()

    # Phase 1: Compression ENABLED
    print("Phase 1: Running with compression ENABLED")
    print("-" * 60)
    enabled_results = []

    for i, task in enumerate(TASKS, 1):
        print(f"[{i}/{len(TASKS)}] {task['id']}")
        result = await run_task_with_compression(
            task, api_key, base_url, model, enable_compression=True
        )
        enabled_results.append(result)

        if result["success"]:
            print(f"  [OK] {result['tokens']:,} tokens, {result['turns']} turns, {result['elapsed']:.1f}s")
        else:
            print(f"  [ERR] {result.get('error', 'Unknown error')}")

    print()

    # Phase 2: Compression DISABLED
    print("Phase 2: Running with compression DISABLED")
    print("-" * 60)
    disabled_results = []

    for i, task in enumerate(TASKS, 1):
        print(f"[{i}/{len(TASKS)}] {task['id']}")
        result = await run_task_with_compression(
            task, api_key, base_url, model, enable_compression=False
        )
        disabled_results.append(result)

        if result["success"]:
            print(f"  [OK] {result['tokens']:,} tokens, {result['turns']} turns, {result['elapsed']:.1f}s")
        else:
            print(f"  [ERR] {result.get('error', 'Unknown error')}")

    print()

    # Calculate metrics
    enabled_tokens = sum(r["tokens"] for r in enabled_results if r["success"])
    disabled_tokens = sum(r["tokens"] for r in disabled_results if r["success"])

    enabled_success = sum(1 for r in enabled_results if r["success"])
    disabled_success = sum(1 for r in disabled_results if r["success"])

    if disabled_tokens > 0:
        reduction_pct = (disabled_tokens - enabled_tokens) / disabled_tokens * 100
    else:
        reduction_pct = 0.0

    # Print summary
    print("=" * 60)
    print("A/B TEST RESULTS")
    print("=" * 60)
    print(f"Compression ENABLED:  {enabled_tokens:,} tokens ({enabled_success}/{len(TASKS)} success)")
    print(f"Compression DISABLED: {disabled_tokens:,} tokens ({disabled_success}/{len(TASKS)} success)")
    print(f"Token reduction:      {reduction_pct:.1f}%")
    print(f"Completion rate (ON): {enabled_success/len(TASKS):.0%}")
    print(f"Completion rate (OFF): {disabled_success/len(TASKS):.0%}")
    print()

    # Check if target met
    if reduction_pct >= 40:
        print("✅ TARGET MET: Token reduction >= 40%")
    else:
        print(f"⚠️  TARGET NOT MET: Token reduction {reduction_pct:.1f}% < 40%")

    # Save results
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "summary": {
            "compression_enabled_tokens": enabled_tokens,
            "compression_disabled_tokens": disabled_tokens,
            "token_reduction_percent": round(reduction_pct, 1),
            "completion_rate_enabled": round(enabled_success / len(TASKS), 2),
            "completion_rate_disabled": round(disabled_success / len(TASKS), 2),
            "target_met": reduction_pct >= 40,
        },
        "enabled_results": enabled_results,
        "disabled_results": disabled_results,
    }

    report_file = output_dir / "ab_test_report.json"
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Report saved: {report_file}")
    print("=" * 60)

    return report


if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://synai996.space")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    output_dir = Path("eval_results/ab_test")

    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        exit(1)

    asyncio.run(run_ab_test(api_key, base_url, model, output_dir))
