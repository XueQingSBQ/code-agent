"""Debug: Check if model is making tool calls."""

import asyncio
from pathlib import Path

from carbide.infra.config import Config
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def main():
    config = Config.load(workspace=Path.cwd())
    registry = create_default_registry()
    tools = registry.api_schemas()

    print(f"Number of tools available: {len(tools)}")
    print("Tools:", [t["name"] for t in tools])
    print("=" * 60)

    agent = Agent(config=config, tool_registry=registry)

    print("Query: 创建一个test.txt文件，内容是'Hello'")
    print("=" * 60)

    async for event in agent.query("创建一个test.txt文件，内容是'Hello'", tools=tools):
        if event.type == "chunk":
            chunk = event.data
            print(f"[Chunk] type={chunk.type}", end="")
            if chunk.type == "content_block_start":
                if chunk.tool_use_id:
                    print(f" tool_use_id={chunk.tool_use_id} tool_name={chunk.tool_name}")
                else:
                    print(" (text block)")
            elif chunk.type == "content_block_delta":
                if chunk.content:
                    print(f" content={chunk.content!r}")
                elif chunk.tool_input:
                    print(f" tool_input={chunk.tool_input!r}")
            else:
                print()
        elif event.type == "tool_complete":
            print(f"\n[TOOL EXECUTED] {event.data['tool_name']} in {event.data['duration_ms']}ms")
        elif event.type == "turn_complete":
            print(f"\n[Turn complete] {event.data}")

    # Check if file was created
    if Path("test.txt").exists():
        print("\n✓ File test.txt created!")
        print(f"Content: {Path('test.txt').read_text()}")
    else:
        print("\n✗ File test.txt NOT created")


asyncio.run(main())
