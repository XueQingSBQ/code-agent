"""Test cache hit tracking in CLI."""

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from carbide.sdk import Agent

# Load environment variables
load_dotenv()


async def main():
    agent = Agent(
        project_dir=Path.cwd(),
        enable_compression=False,  # Disable compression for clearer test
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    print("Query 1 (should NOT hit cache):")
    print("=" * 60)

    async for event in agent.query("你好"):
        if event.type == "chunk" and event.data.content:
            print(event.data.content, end="", flush=True)
        elif event.type == "turn_complete":
            print(f"\n\nTurn stats: {event.data}")

    # Get agent stats
    stats = agent.get_stats()
    print(f"\nAgent stats after turn 1:")
    print(f"  Input tokens: {stats['total_input_tokens']}")
    print(f"  Output tokens: {stats['total_output_tokens']}")
    print(f"  Cache creation: {stats['total_cache_creation_tokens']}")
    print(f"  Cache read: {stats['total_cache_read_tokens']}")

    print("\n\n" + "=" * 60)
    print("Query 2 (SHOULD hit cache):")
    print("=" * 60)

    async for event in agent.query("你好"):
        if event.type == "chunk" and event.data.content:
            print(event.data.content, end="", flush=True)
        elif event.type == "turn_complete":
            print(f"\n\nTurn stats: {event.data}")

    # Get final stats
    stats = agent.get_stats()
    print(f"\nAgent stats after turn 2:")
    print(f"  Input tokens: {stats['total_input_tokens']}")
    print(f"  Output tokens: {stats['total_output_tokens']}")
    print(f"  Cache creation: {stats['total_cache_creation_tokens']}")
    print(f"  Cache read: {stats['total_cache_read_tokens']}")

    # Calculate hit rate
    total_cache_read = stats['total_cache_read_tokens']
    total_input = stats['total_input_tokens'] + total_cache_read
    hit_rate = (total_cache_read / total_input * 100) if total_input else 0.0

    print(f"\n{'=' * 60}")
    print(f"Cache hit rate: {hit_rate:.1f}%")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
