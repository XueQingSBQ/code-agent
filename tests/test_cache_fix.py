"""Test cache hit tracking fix."""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from carbide.sdk import Agent

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# Load environment variables
load_dotenv()


async def main():
    from carbide.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        api_key="pwd",
        model="gpt-5.6-luna",
        base_url="http://127.0.0.1:8080",
    )

    agent = Agent(
        provider=provider,
        project_dir=Path.cwd(),
        enable_compression=False,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    print("Query 1 (should create cache):")
    print("=" * 60)

    async for event in agent.query("Say hello in one word"):
        if event.type == "chunk" and event.data.content:
            print(event.data.content, end="", flush=True)
        elif event.type == "turn_complete":
            print(f"\n\nTurn 1 stats:")
            print(f"  input_tokens: {event.data['input_tokens']}")
            print(f"  output_tokens: {event.data['output_tokens']}")
            print(f"  cache_read: {event.data['cache_read_tokens']}")
            print(f"  cache_creation: {event.data['cache_creation_tokens']}")
        elif event.type == "error":
            print(f"\nError: {event.data}")
            return

    print("\n" + "=" * 60)
    print("Query 2 (should hit cache):")
    print("=" * 60)

    async for event in agent.query("Say goodbye in one word"):
        if event.type == "chunk" and event.data.content:
            print(event.data.content, end="", flush=True)
        elif event.type == "turn_complete":
            print(f"\n\nTurn 2 stats:")
            print(f"  input_tokens: {event.data['input_tokens']}")
            print(f"  output_tokens: {event.data['output_tokens']}")
            print(f"  cache_read: {event.data['cache_read_tokens']}")
            print(f"  cache_creation: {event.data['cache_creation_tokens']}")
        elif event.type == "error":
            print(f"\nError: {event.data}")
            return

    # Get cumulative stats
    stats = agent.get_stats()
    print(f"\n{'=' * 60}")
    print(f"Cumulative stats:")
    print(f"  Total input: {stats['total_input_tokens']}")
    print(f"  Total output: {stats['total_output_tokens']}")
    print(f"  Total cache creation: {stats['total_cache_creation_tokens']}")
    print(f"  Total cache read: {stats['total_cache_read_tokens']}")

    # Calculate hit rate
    total_cache_read = stats['total_cache_read_tokens']
    total_input = stats['total_input_tokens'] + total_cache_read
    hit_rate = (total_cache_read / total_input * 100) if total_input else 0.0

    print(f"\n  Cache hit rate: {hit_rate:.1f}%")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
