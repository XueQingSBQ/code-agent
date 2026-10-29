"""Test CLI agent directly with error handling."""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from carbide.infra.config import Config
from carbide.sdk import Agent


async def main():
    # Load environment
    load_dotenv()

    print(f"API Key: {os.getenv('ANTHROPIC_API_KEY')[:20]}...")
    print(f"Base URL: {os.getenv('ANTHROPIC_BASE_URL')}")

    # Load config
    config = Config.load()
    print(f"Config model: {config.model}")
    print(f"Config base_url: {config.base_url}")

    # Create agent
    print("\nCreating agent...")
    async with Agent(config=config) as agent:
        print("Agent created successfully")

        # Test query
        print("\nSending query...")
        try:
            async for event in agent.query("What is 2+2?"):
                print(f"Event: type={event.type}, data={event.data}")
                if event.type == "error":
                    print(f"ERROR: {event.data}")
                    break
        except Exception as e:
            print(f"Exception during query: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
