"""Debug: Check what events are being emitted."""

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from carbide.sdk import Agent

# Load environment variables
load_dotenv()


async def main():
    agent = Agent(
        project_dir=Path.cwd(),
        enable_compression=False,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    print("Sending query and inspecting events...")
    print("=" * 60)

    async for event in agent.query("Hello"):
        print(f"Event type: {event.type}")
        if event.type == "chunk":
            print(f"  Chunk: {event.data}")
            if hasattr(event.data, 'usage') and event.data.usage:
                print(f"  Usage: {event.data.usage}")
        elif event.type == "turn_complete":
            print(f"  Turn data: {event.data}")
        elif event.type == "error":
            print(f"  Error: {event.data}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
