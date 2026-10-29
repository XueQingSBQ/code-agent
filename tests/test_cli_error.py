"""Debug CLI provider error."""

import asyncio
import traceback
from carbide.infra.config import Config
from carbide.sdk import Agent


async def test():
    """Test agent query with detailed error output."""

    print("Loading config...")
    config = Config.load()

    print(f"API Key: {config.api_key[:10]}..." if config.api_key else "None")
    print(f"Base URL: {config.base_url}")
    print(f"Model: {config.model}")

    print("\nCreating agent...")
    agent = Agent(config=config)

    print("\nSending query...")
    try:
        async for event in agent.query("你好，你是谁"):
            print(f"Event: {event.type}")
            if event.type == "error":
                print(f"Error data: {event.data}")
            elif event.type == "chunk":
                if event.data and event.data.content:
                    print(f"Content: {event.data.content}")
    except Exception as e:
        print(f"\nException: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test())
