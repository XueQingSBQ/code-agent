"""
Debug token tracking at provider level.
"""

import asyncio
import os

from carbide.providers.anthropic import AnthropicProvider


async def test_provider_chunks():
    """Test that provider returns chunks with usage."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://synai996.space")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        return

    print("=" * 60)
    print("TESTING PROVIDER CHUNK STRUCTURE")
    print("=" * 60)
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print()

    # Create provider
    provider = AnthropicProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    # Simple message
    messages = [{"role": "user", "content": "Say 'test' in one word."}]

    print("Sending query...")
    print("-" * 60)

    chunk_count = 0
    found_usage = False

    async for chunk in provider.stream(messages=messages, max_tokens=100):
        chunk_count += 1
        print(f"Chunk {chunk_count}: {chunk.type}")
        print(f"  Full chunk: {chunk}")

        if chunk.usage:
            print(f"  [!!!] FOUND USAGE: {chunk.usage}")
            print(f"    input_tokens: {chunk.usage.input_tokens}")
            print(f"    output_tokens: {chunk.usage.output_tokens}")
            found_usage = True
        else:
            print(f"  No usage")

        print()

    print("-" * 60)
    print(f"Total chunks: {chunk_count}")
    print(f"Found usage: {found_usage}")
    print()

    if found_usage:
        print("[OK] Provider correctly returns usage in chunks")
        return True
    else:
        print("[FAIL] Provider does not return usage in chunks")
        return False


if __name__ == "__main__":
    success = asyncio.run(test_provider_chunks())
    exit(0 if success else 1)
