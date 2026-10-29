"""Test CLI token tracking with multiple queries."""

import asyncio
from carbide.infra.config import Config
from carbide.sdk import Agent


async def test_tokens():
    """Test token tracking across multiple queries."""

    config = Config.load()

    async with Agent(config=config) as agent:
        print("Query 1: What is 2+2?")
        print("=" * 60)

        async for event in agent.query("What is 2+2?"):
            if event.type == "chunk" and event.data:
                if event.data.type == "content_block_delta" and event.data.content:
                    print(event.data.content, end="", flush=True)
            elif event.type == "turn_complete":
                print()
                if isinstance(event.data, dict):
                    print(f"\nTokens: in={event.data['input_tokens']} "
                          f"out={event.data['output_tokens']}")

        print("\n")
        print("Query 2: What is 3+3?")
        print("=" * 60)

        async for event in agent.query("What is 3+3?"):
            if event.type == "chunk" and event.data:
                if event.data.type == "content_block_delta" and event.data.content:
                    print(event.data.content, end="", flush=True)
            elif event.type == "turn_complete":
                print()
                if isinstance(event.data, dict):
                    print(f"\nTokens: in={event.data['input_tokens']} "
                          f"out={event.data['output_tokens']}")

        # Show cumulative stats
        print("\n" + "=" * 60)
        stats = agent.get_stats()
        print(f"Total stats:")
        print(f"  Turns: {stats['turn_count']}")
        print(f"  Total tokens: {stats['total_tokens']}")
        print(f"  Input: {stats['total_input_tokens']}")
        print(f"  Output: {stats['total_output_tokens']}")


if __name__ == "__main__":
    asyncio.run(test_tokens())
