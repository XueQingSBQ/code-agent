"""
Simplified A/B test using provider's usage data directly.

This version tracks token usage from the provider's response metadata
rather than relying on agent stats.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from carbide.providers.anthropic import AnthropicProvider


# Simplified prompts for quick testing
PROMPTS = [
    "Write a Python function that validates an email address using regex. Include docstring and type hints.",
    "Create a JavaScript function that debounces a callback. Add JSDoc comments and handle edge cases.",
    "Write a SQL query that finds the top 10 customers by total order value in the last 30 days.",
]


async def run_with_messages(provider, messages, max_tokens=2000):
    """Run a query and track token usage from provider response."""

    total_input = 0
    total_output = 0
    response_text = []

    async for chunk in provider.stream(
        messages=messages,
        system="You are a helpful coding assistant.",
        max_tokens=max_tokens,
    ):
        # Collect response text
        if chunk.type == "content_block_delta" and chunk.content:
            response_text.append(chunk.content)

        # Provider should emit usage in final message_stop or message_delta
        if hasattr(chunk, 'usage'):
            if hasattr(chunk.usage, 'input_tokens'):
                total_input = chunk.usage.input_tokens
            if hasattr(chunk.usage, 'output_tokens'):
                total_output = chunk.usage.output_tokens

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "response": "".join(response_text),
    }


async def simulate_multi_turn_with_compression(provider, prompt, enable_compression=True):
    """
    Simulate a multi-turn conversation to test compression.

    In a real scenario, compression would kick in after several turns.
    For this test, we'll simulate a long context scenario.
    """

    messages = [{"role": "user", "content": prompt}]

    # First turn
    result1 = await run_with_messages(provider, messages)

    # Add assistant response to history
    messages.append({
        "role": "assistant",
        "content": result1["response"],
    })

    # Second turn - ask for modification
    messages.append({
        "role": "user",
        "content": "Now add comprehensive error handling and unit tests.",
    })

    result2 = await run_with_messages(provider, messages)

    # Add second response
    messages.append({
        "role": "assistant",
        "content": result2["response"],
    })

    # Third turn - ask for documentation
    messages.append({
        "role": "user",
        "content": "Add detailed documentation with examples.",
    })

    result3 = await run_with_messages(provider, messages)

    # Total tokens
    total_tokens = result1["total_tokens"] + result2["total_tokens"] + result3["total_tokens"]

    return {
        "turns": 3,
        "total_tokens": total_tokens,
        "turn1_tokens": result1["total_tokens"],
        "turn2_tokens": result2["total_tokens"],
        "turn3_tokens": result3["total_tokens"],
    }


async def run_simple_ab_test(api_key, base_url, model, output_dir):
    """
    Run simplified A/B test.

    Note: Current SDK doesn't have compression fully implemented yet,
    so this test will show baseline measurements.
    """

    print("=" * 60)
    print("SIMPLIFIED A/B TEST - TOKEN TRACKING")
    print("=" * 60)
    print(f"Model: {model}")
    print(f"Prompts: {len(PROMPTS)}")
    print()

    # Create provider
    provider = AnthropicProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    # Run tests
    print("Running multi-turn conversations...")
    print("-" * 60)

    results = []

    for i, prompt in enumerate(PROMPTS, 1):
        print(f"[{i}/{len(PROMPTS)}] Processing prompt {i}")
        result = await simulate_multi_turn_with_compression(provider, prompt)
        results.append(result)
        print(f"  Total: {result['total_tokens']:,} tokens over {result['turns']} turns")
        print(f"  Turn breakdown: {result['turn1_tokens']} -> {result['turn2_tokens']} -> {result['turn3_tokens']}")

    print()

    # Calculate totals
    total_tokens = sum(r["total_tokens"] for r in results)
    avg_tokens = total_tokens / len(results)

    print("=" * 60)
    print("BASELINE MEASUREMENTS")
    print("=" * 60)
    print(f"Total tokens: {total_tokens:,}")
    print(f"Avg tokens/conversation: {avg_tokens:,.0f}")
    print(f"Total conversations: {len(results)}")
    print()

    # Note about compression
    print("NOTE: Context compression is not fully implemented in the current SDK.")
    print("These are baseline measurements. Full A/B test requires:")
    print("  1. Implement compression manager in SDK")
    print("  2. Run same tasks with compression ON vs OFF")
    print("  3. Compare token usage")
    print()

    # Save results
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "type": "baseline_measurement",
        "summary": {
            "total_tokens": total_tokens,
            "avg_tokens_per_conversation": round(avg_tokens, 0),
            "num_conversations": len(results),
        },
        "results": results,
        "note": "Baseline measurements without compression. Full A/B test requires compression implementation.",
    }

    report_file = output_dir / "baseline_tokens.json"
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

    asyncio.run(run_simple_ab_test(api_key, base_url, model, output_dir))
