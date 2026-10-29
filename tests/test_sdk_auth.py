"""Debug SDK authentication."""

import asyncio
import anthropic


async def main():
    print("Testing Anthropic SDK...")

    client = anthropic.AsyncAnthropic(
        api_key="pwd",
        base_url="http://127.0.0.1:8080",
    )

    print(f"Client configured:")
    print(f"  Base URL: {client.base_url}")
    print(f"  Default headers: {client.default_headers}")

    try:
        print("\nSending request...")
        response = await client.messages.create(
            model="gpt-5.6-luna",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        print(f"Success!")
        print(f"Response: {response.content[0].text}")
        print(f"Usage: {response.usage}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
