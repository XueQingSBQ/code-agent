"""
Simple test for Carbide Agent.
"""

import asyncio
import os

from carbide.sdk import Agent


async def test_agent():
    """Test basic agent functionality."""

    print("=" * 60)
    print("Carbide Agent - Simple Test")
    print("=" * 60)
    print()

    # Create agent
    print("Creating agent...")
    agent = Agent(
        enable_compression=True,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )
    print("Agent created successfully!")
    print()

    # Test query
    queries = [
        "Write a Python function to calculate factorial",
        "What is 5 factorial?",
    ]

    for i, query in enumerate(queries, 1):
        print(f"Query {i}: {query}")
        print("-" * 60)
        print("Response: ", end="", flush=True)

        try:
            async for event in agent.query(query):
                if event.type == "chunk" and event.data:
                    if hasattr(event.data, 'content') and event.data.content:
                        print(event.data.content, end="", flush=True)
                elif event.type == "error":
                    print(f"\nError: {event.data}")
                    break

            print("\n")

        except Exception as e:
            print(f"\nException: {e}")
            import traceback
            traceback.print_exc()
            print()

    # Show statistics
    print("=" * 60)
    print("Statistics")
    print("=" * 60)
    stats = agent.get_stats()
    print(f"Turns: {stats['turn_count']}")
    print(f"Total tokens: {stats['total_tokens']}")
    print(f"Input tokens: {stats['total_input_tokens']}")
    print(f"Output tokens: {stats['total_output_tokens']}")
    print(f"Compression: {'enabled' if stats['compression_enabled'] else 'disabled'}")
    print()

    print("Test completed successfully!")


if __name__ == "__main__":
    asyncio.run(test_agent())
