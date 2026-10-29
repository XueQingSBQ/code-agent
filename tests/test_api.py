"""Test script to verify API configuration."""

import asyncio
import os

import pytest

from carbide.providers.anthropic import create_anthropic_provider


@pytest.mark.live
async def test_api_connection():
    """Test connection to API."""

    print("=" * 60)
    print("Testing API Connection")
    print("=" * 60)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY is required for the live API check")

    base_url = os.environ.get("ANTHROPIC_BASE_URL")

    # Create provider using the configured model so the live test exercises the
    # same endpoint/model pair as the CLI.
    provider = create_anthropic_provider(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    )

    print("\nProvider configured:")
    print(f"  Model: {provider.model}")
    print(f"  Base URL: {base_url or 'Anthropic default'}")
    print("  API Key: configured (redacted)")
    print(f"  Client base_url: {provider.client.base_url}")
    print()

    # Test simple query
    print("Sending test query: 'Hello, how are you?'")
    print("-" * 60)

    try:
        messages = [
            {"role": "user", "content": "Hello, how are you?"}
        ]

        response_chunks: list[str] = []
        async for chunk in provider.stream(messages=messages, max_tokens=100):
            # The provider seam emits StreamTextDelta.text. Keep the fallback
            # for older compatible providers without weakening the assertion.
            content = getattr(chunk, "text", None) or getattr(chunk, "content", None)
            if isinstance(content, str) and content:
                print(content, end="", flush=True)
                response_chunks.append(content)

        print("\n" + "-" * 60)
        print(f"\nSuccess! Received {len(response_chunks)} chunks")
        print(f"Total response length: {sum(len(c) for c in response_chunks)} chars")
        assert response_chunks, "live API returned no text deltas"

    except Exception as e:
        print("\n" + "-" * 60)
        print(f"\nError: {e}")
        print(f"\nError type: {type(e).__name__}")

        import traceback

        print("\nFull traceback:")
        traceback.print_exc()
        pytest.fail(f"live API request failed: {type(e).__name__}: {e}")
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()


if __name__ == "__main__":
    asyncio.run(test_api_connection())
