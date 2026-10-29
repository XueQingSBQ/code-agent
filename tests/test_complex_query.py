"""Test complex query with error handling."""

import asyncio
from carbide.infra.config import Config
from carbide.sdk import Agent


async def main():
    config = Config.load()

    async with Agent(config=config) as agent:
        print("Sending complex query...")
        try:
            async for event in agent.query("Write a Python function to calculate factorial"):
                if event.type == "chunk" and event.data:
                    if event.data.type == "content_block_delta" and event.data.content:
                        print(event.data.content, end="", flush=True)
                elif event.type == "error":
                    print(f"\nERROR: {event.data}")
                    break
            print("\n\nQuery completed")
        except Exception as e:
            print(f"\nException: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
