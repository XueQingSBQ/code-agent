"""Debug: Check if API returns cache info."""

import asyncio
import anthropic


async def main():
    client = anthropic.AsyncAnthropic(
        base_url="http://127.0.0.1:8080",
        api_key="pwd",
    )

    print("First query (should NOT hit cache):")
    print("=" * 60)

    async with client.messages.stream(
        model="gpt-5.6-luna",
        max_tokens=64,
        system=[
            {
                "type": "text",
                "text": "You are a helpful assistant.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": "Say hello"}],
    ) as stream:
        async for text in stream.text_stream:
            try:
                print(text, end="", flush=True)
            except UnicodeEncodeError:
                print(text.encode('utf-8', errors='ignore').decode('utf-8'), end="", flush=True)

        # Get final message with usage
        message = await stream.get_final_message()
        print(f"\n\nUsage: {message.usage}")
        print(f"Usage dict: {message.usage.model_dump() if message.usage else 'None'}")

    print("\n\n" + "=" * 60)
    print("Second query (SHOULD hit cache if supported):")
    print("=" * 60)

    async with client.messages.stream(
        model="gpt-5.6-luna",
        max_tokens=64,
        system=[
            {
                "type": "text",
                "text": "You are a helpful assistant.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": "Say goodbye"}],
    ) as stream:
        async for text in stream.text_stream:
            try:
                print(text, end="", flush=True)
            except UnicodeEncodeError:
                print(text.encode('utf-8', errors='ignore').decode('utf-8'), end="", flush=True)

        message = await stream.get_final_message()
        print(f"\n\nUsage: {message.usage}")
        print(f"Usage dict: {message.usage.model_dump() if message.usage else 'None'}")

        # Check for cache fields
        if message.usage:
            usage_dict = message.usage.model_dump()
            print(f"\nCache-related fields:")
            for key in usage_dict:
                if 'cache' in key.lower():
                    print(f"  {key}: {usage_dict[key]}")

    await client.close()


asyncio.run(main())
