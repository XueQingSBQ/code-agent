"""Test AnthropicProvider directly."""

import asyncio
import sys
from dotenv import load_dotenv

from carbide.providers.anthropic import AnthropicProvider

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()


async def main():
    print("Testing AnthropicProvider...")

    provider = AnthropicProvider(
        api_key="pwd",
        model="gpt-5.6-luna",
        base_url="http://127.0.0.1:8080",
    )

    print(f"Provider configured:")
    print(f"  Model: {provider.model}")
    print(f"  Client base_url: {provider.client.base_url}")

    try:
        print("\nSending request...")
        async for chunk in provider.stream(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=10,
        ):
            print(f"Chunk: {chunk}")
            if chunk.content:
                print(chunk.content, end="", flush=True)

        print("\n\nSuccess!")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
