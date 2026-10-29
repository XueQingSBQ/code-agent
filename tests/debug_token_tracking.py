"""
Debug script to inspect Anthropic API event stream and usage information.
"""

import asyncio
import os

from anthropic import AsyncAnthropic


async def debug_event_stream():
    """Debug raw event stream from Anthropic API."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://synai996.space")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        return

    print("=" * 60)
    print("DEBUGGING ANTHROPIC EVENT STREAM")
    print("=" * 60)
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print()

    # Create client
    client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    # Simple message
    messages = [{"role": "user", "content": "Say 'hello' in one word."}]

    print("Sending query: Say 'hello' in one word.")
    print("-" * 60)
    print()

    try:
        async with client.messages.stream(
            model=model,
            messages=messages,
            max_tokens=100,
        ) as stream:
            event_count = 0
            async for event in stream:
                event_count += 1
                print(f"Event {event_count}: {event.type}")
                print(f"  Full event: {event}")

                # Check for usage information
                if hasattr(event, 'usage'):
                    print(f"  [!!!] HAS USAGE: {event.usage}")
                    if event.usage:
                        print(f"    - input_tokens: {getattr(event.usage, 'input_tokens', 'N/A')}")
                        print(f"    - output_tokens: {getattr(event.usage, 'output_tokens', 'N/A')}")
                else:
                    print(f"  No usage attribute")

                # Check for delta
                if hasattr(event, 'delta'):
                    print(f"  Delta: {event.delta}")

                # Check for content_block
                if hasattr(event, 'content_block'):
                    print(f"  Content block: {event.content_block}")

                # Check for message
                if hasattr(event, 'message'):
                    print(f"  Message: {event.message}")

                print()

        print("-" * 60)
        print(f"Total events: {event_count}")

        # Also check final message
        print()
        print("Checking final message object...")
        final_message = await stream.get_final_message()
        print(f"Final message: {final_message}")
        print(f"Usage: {final_message.usage}")
        print(f"  input_tokens: {final_message.usage.input_tokens}")
        print(f"  output_tokens: {final_message.usage.output_tokens}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    print()
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(debug_event_stream())
