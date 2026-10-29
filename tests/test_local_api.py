"""Test local API endpoint."""

import asyncio
from carbide import Agent
from carbide.providers.anthropic import AnthropicProvider


async def test_local_api():
    """Test with local API."""

    # Create provider with local endpoint
    provider = AnthropicProvider(
        api_key="pwd",
        base_url="http://127.0.0.1:8080",
        model="gpt-5.6-luna",
    )

    # Create agent
    agent = Agent(
        provider=provider,
        enable_compression=True,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    print("Testing local API at http://127.0.0.1:8080")
    print("=" * 60)
    print("Agent: ", end="", flush=True)

    try:
        async for event in agent.query("你好，请回复：LOCAL_LUNA_ASYNC_OK"):
            if event.type == "chunk" and event.data:
                if hasattr(event.data, 'content') and event.data.content:
                    print(event.data.content, end="", flush=True)
            elif event.type == "turn_complete":
                print()
                if isinstance(event.data, dict):
                    print(f"\nTokens: in={event.data['input_tokens']} "
                          f"out={event.data['output_tokens']}")

        print("\n[OK] Local API works!")

    except Exception as e:
        print(f"\n[FAIL] Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_local_api())
