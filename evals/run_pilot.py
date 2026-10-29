"""
Simplified SWE-bench pilot runner (no Docker).

This is a lightweight version that:
1. Uses the sample dataset (3 instances)
2. Runs Carbide agent on each problem
3. Collects basic metrics
4. Generates a pilot report

For full evaluation with Docker isolation, use the main CLI.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from carbide.providers.anthropic import create_anthropic_provider
from carbide.sdk import Agent


async def run_pilot_instance(
    instance: dict[str, Any],
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Run a single SWE-bench instance (simplified)."""

    instance_id = instance["instance_id"]
    problem = instance["problem_statement"]

    print(f"\n{'='*60}")
    print(f"Instance: {instance_id}")
    print(f"{'='*60}")
    print(f"Problem: {problem[:200]}...")

    # Create provider
    provider = create_anthropic_provider(
        model=model,
        api_key=api_key,
    )

    # Create agent
    agent = Agent(provider=provider)

    # Build query
    query = f"""You are solving a GitHub issue. Here's the problem:

{problem}

Repository: {instance['repo']}
Base commit: {instance['base_commit']}

Please analyze the problem and propose a fix. Focus on:
1. Understanding the root cause
2. Identifying which files need to change
3. Proposing the fix

Keep your response focused and technical.
"""

    # Run agent
    start_time = time.time()
    response_chunks = []

    try:
        async for event in agent.query(query):
            # Handle different event types
            if event.type == "chunk" and event.data:
                # Check if this is a text delta (content_block_delta with text)
                if hasattr(event.data, 'type') and event.data.type == "content_block_delta":
                    if hasattr(event.data, 'content') and event.data.content:
                        content = event.data.content
                        # Handle encoding for Windows console
                        try:
                            print(content, end="", flush=True)
                        except UnicodeEncodeError:
                            # Fallback to ASCII representation for problematic characters
                            print(content.encode('ascii', 'replace').decode('ascii'), end="", flush=True)
                        response_chunks.append(content)

        elapsed = time.time() - start_time
        response = "".join(response_chunks)

        # Estimate tokens (rough)
        input_tokens = len(query) // 4
        output_tokens = len(response) // 4
        total_tokens = input_tokens + output_tokens

        # Estimate cost (Sonnet-4 pricing: $3/$15 per 1M tokens)
        cost = (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000

        result = {
            "instance_id": instance_id,
            "status": "completed",
            "response_length": len(response),
            "elapsed_seconds": elapsed,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens,
            },
            "cost_usd": cost,
            "response_preview": response[:500] + "..." if len(response) > 500 else response,
        }

        print(f"\n\n[Completed in {elapsed:.1f}s, ~{total_tokens:,} tokens, ~${cost:.3f}]")
        return result

    except Exception as e:
        print(f"\n\n[ERROR: {e}]")
        return {
            "instance_id": instance_id,
            "status": "error",
            "error": str(e),
            "elapsed_seconds": time.time() - start_time,
        }


async def run_pilot_evaluation(
    dataset_path: Path,
    output_dir: Path,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    max_instances: int = 3,
):
    """Run pilot evaluation on a small dataset."""

    print("="*60)
    print("SWE-BENCH PILOT EVALUATION")
    print("="*60)
    print(f"Model: {model}")
    print(f"Max instances: {max_instances}")
    print(f"Dataset: {dataset_path}")
    print(f"Output: {output_dir}")
    print("="*60)

    # Load dataset
    with open(dataset_path) as f:
        instances = json.load(f)

    # Limit instances
    instances = instances[:max_instances]
    print(f"\nLoaded {len(instances)} instances")

    # Run each instance
    results = []
    for i, instance in enumerate(instances, 1):
        print(f"\n\nProgress: {i}/{len(instances)}")
        result = await run_pilot_instance(instance, api_key, model)
        results.append(result)

    # Calculate aggregate metrics
    completed = [r for r in results if r["status"] == "completed"]

    if completed:
        total_tokens = sum(r["tokens"]["total"] for r in completed)
        total_cost = sum(r["cost_usd"] for r in completed)
        avg_tokens = total_tokens / len(completed)
        avg_cost = total_cost / len(completed)
        avg_time = sum(r["elapsed_seconds"] for r in completed) / len(completed)
    else:
        total_tokens = total_cost = avg_tokens = avg_cost = avg_time = 0

    aggregate = {
        "total_instances": len(instances),
        "completed": len(completed),
        "errors": len(results) - len(completed),
        "completion_rate": len(completed) / len(instances) * 100,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "avg_tokens_per_instance": avg_tokens,
        "avg_cost_per_instance": avg_cost,
        "avg_time_per_instance": avg_time,
    }

    # Generate report
    report = {
        "pilot_evaluation": True,
        "model": model,
        "dataset": str(dataset_path),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "aggregate": aggregate,
        "results": results,
        "note": "This is a pilot evaluation without Docker isolation and without test validation. For full evaluation, use the main CLI with Docker.",
    }

    # Save report
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "pilot_report.json"

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary
    print("\n\n" + "="*60)
    print("PILOT EVALUATION SUMMARY")
    print("="*60)
    print(f"Total instances: {aggregate['total_instances']}")
    print(f"Completed: {aggregate['completed']}")
    print(f"Errors: {aggregate['errors']}")
    print(f"Completion rate: {aggregate['completion_rate']:.1f}%")
    print()
    print(f"Total tokens: {aggregate['total_tokens']:,}")
    print(f"Total cost: ${aggregate['total_cost_usd']:.2f}")
    print()
    print(f"Avg tokens/instance: {aggregate['avg_tokens_per_instance']:,.0f}")
    print(f"Avg cost/instance: ${aggregate['avg_cost_per_instance']:.3f}")
    print(f"Avg time/instance: {aggregate['avg_time_per_instance']:.1f}s")
    print()
    print(f"Report saved: {report_path}")
    print("="*60)

    return report


async def main():
    """Main entry point."""
    import os

    # Get API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        return

    # Paths
    dataset_path = Path("evals/swebench/sample_dataset.json")
    output_dir = Path("eval_results/pilot")

    # Run evaluation
    await run_pilot_evaluation(
        dataset_path=dataset_path,
        output_dir=output_dir,
        api_key=api_key,
        model="claude-sonnet-4-6",  # Use user's model
        max_instances=3,
    )


if __name__ == "__main__":
    asyncio.run(main())
