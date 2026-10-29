"""
End-to-end test to debug where token tracking breaks.
"""

import asyncio
import os

from carbide.providers.anthropic import AnthropicProvider
from carbide.sdk import Agent


async def test_query_loop_token_tracking():
    """Test token tracking through the entire stack."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://synai996.space")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        return

    print("=" * 60)
    print("END-TO-END TOKEN TRACKING TEST")
    print("=" * 60)
    print()

    # Create provider
    provider = AnthropicProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    # Create agent
    agent = Agent(
        provider=provider,
        enable_compression=True,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    print("Sending query through Agent...")
    print("-" * 60)

    # Track events
    events = []
    response_chunks = []

    async for event in agent.query("Say 'hello' in one word"):
        events.append(event)

        print(f"Event: {event.type}")

        if event.type == "chunk" and event.data:
            print(f"  Chunk type: {event.data.type}")

            # Check for usage in chunk
            if hasattr(event.data, 'usage') and event.data.usage:
                print(f"  [!!!] Chunk has usage: {event.data.usage}")

            if event.data.type == "content_block_delta" and event.data.content:
                response_chunks.append(event.data.content)
                print(f"  Content: {event.data.content}")

    print()
    print("-" * 60)
    print(f"Total events: {len(events)}")
    print(f"Response: {''.join(response_chunks)}")
    print()

    # Check agent state
    state = agent.loop.get_state()
    print("Agent State:")
    print(f"  turn_count: {state.turn_count}")
    print(f"  total_input_tokens: {state.total_input_tokens}")
    print(f"  total_output_tokens: {state.total_output_tokens}")
    print(f"  messages: {len(state.messages)}")
    print()

    # Check stats
    stats = agent.get_stats()
    print("Agent Stats:")
    print(f"  turn_count: {stats['turn_count']}")
    print(f"  total_input_tokens: {stats['total_input_tokens']}")
    print(f"  total_output_tokens: {stats['total_output_tokens']}")
    print(f"  total_tokens: {stats['total_tokens']}")
    print()

    # Verify
    if stats['total_tokens'] > 0:
        print("[OK] Token tracking works!")
        return True
    else:
        print("[FAIL] Token tracking broken - tokens still 0")
        print()
        print("Debug info:")
        print(f"  Events received: {len(events)}")
        print(f"  Event types: {[e.type for e in events]}")

        # Check if any event had usage
        found_usage_in_events = False
        for i, event in enumerate(events):
            if event.type == "chunk" and hasattr(event.data, 'usage') and event.data.usage:
                print(f"  Event {i} HAS usage: {event.data.usage}")
                found_usage_in_events = True

        if not found_usage_in_events:
            print("  No events had usage information!")

        return False


if __name__ == "__main__":
    success = asyncio.run(test_query_loop_token_tracking())
    exit(0 if success else 1)
