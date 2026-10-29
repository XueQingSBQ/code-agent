"""
Test compression and token tracking fixes.
"""

import asyncio
import os

from carbide.providers.anthropic import AnthropicProvider
from carbide.sdk import Agent


async def test_token_tracking():
    """Test that token tracking works correctly."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")  # Changed from claude-sonnet-4-20250514

    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        return False

    print("Testing token tracking...")
    print("-" * 60)

    # Create provider
    provider = AnthropicProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    # Create agent with compression enabled
    agent = Agent(
        provider=provider,
        enable_compression=True,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    # Send a simple query
    print("Query: Write a Python function that adds two numbers")

    response_text = []
    event_count = 0
    found_usage = False

    try:
        async for event in agent.query("Write a Python function that adds two numbers"):
            event_count += 1
            print(f"  Event {event_count}: type={event.type}, data={type(event.data).__name__ if event.data else None}")

            if event.type == "chunk" and event.data:
                if hasattr(event.data, 'usage') and event.data.usage:
                    print(f"    [!!!] Found usage: {event.data.usage}")
                    found_usage = True
                if hasattr(event.data, 'type') and event.data.type == "content_block_delta":
                    if hasattr(event.data, 'content') and event.data.content:
                        response_text.append(event.data.content)
            elif event.type == "error":
                print(f"    [ERROR] {event.data}")
    except Exception as e:
        print(f"  Exception during query: {e}")
        import traceback
        traceback.print_exc()

    print(f"  Total events: {event_count}")
    print(f"  Found usage: {found_usage}")

    # Get stats
    stats = agent.get_stats()

    print()
    print("Response length:", len("".join(response_text)), "chars")
    print()
    print("Stats:")
    print(f"  Turn count: {stats['turn_count']}")
    print(f"  Input tokens: {stats['total_input_tokens']}")
    print(f"  Output tokens: {stats['total_output_tokens']}")
    print(f"  Total tokens: {stats['total_tokens']}")
    print(f"  Compression enabled: {stats['compression_enabled']}")
    print()

    # Verify tokens are tracked
    if stats['total_tokens'] > 0:
        print("[OK] Token tracking working!")
        return True
    else:
        print("[FAIL] Token tracking NOT working - tokens still 0")
        return False


async def test_compression_enabled():
    """Test that compression manager is enabled."""

    print("\nTesting compression manager initialization...")
    print("-" * 60)

    # Create agent with compression enabled
    agent = Agent(enable_compression=True)

    if agent.compression_manager is not None:
        print("[OK] Compression manager initialized")
        print(f"  Budget: {agent.compression_manager.budget.max_tokens} tokens")
        print(f"  Threshold: {agent.compression_manager.budget.compression_threshold}")
        return True
    else:
        print("[FAIL] Compression manager is None")
        return False


async def test_compression_disabled():
    """Test that compression can be disabled."""

    print("\nTesting compression disabled...")
    print("-" * 60)

    # Create agent with compression disabled
    agent = Agent(enable_compression=False)

    if agent.compression_manager is None:
        print("[OK] Compression correctly disabled")
        return True
    else:
        print("[FAIL] Compression manager should be None when disabled")
        return False


async def main():
    """Run all tests."""

    print("=" * 60)
    print("COMPRESSION AND TOKEN TRACKING TESTS")
    print("=" * 60)
    print()

    results = []

    # Test 1: Compression enabled
    try:
        result = await test_compression_enabled()
        results.append(("Compression enabled", result))
    except Exception as e:
        print(f"[FAIL] Error: {e}")
        results.append(("Compression enabled", False))

    # Test 2: Compression disabled
    try:
        result = await test_compression_disabled()
        results.append(("Compression disabled", result))
    except Exception as e:
        print(f"[FAIL] Error: {e}")
        results.append(("Compression disabled", False))

    # Test 3: Token tracking (requires API key)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            result = await test_token_tracking()
            results.append(("Token tracking", result))
        except Exception as e:
            print(f"[FAIL] Error: {e}")
            import traceback
            traceback.print_exc()
            results.append(("Token tracking", False))
    else:
        print("\nSkipping token tracking test (no API key)")
        results.append(("Token tracking", None))

    # Summary
    print()
    print("=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    for name, result in results:
        if result is True:
            status = "[OK] PASS"
        elif result is False:
            status = "[FAIL] FAIL"
        else:
            status = "[-] SKIP"
        print(f"{status}: {name}")

    passed = sum(1 for _, r in results if r is True)
    total = sum(1 for _, r in results if r is not None)

    print()
    print(f"Result: {passed}/{total} tests passed")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
