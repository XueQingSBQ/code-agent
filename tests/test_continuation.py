"""Debug: Why no output on second request."""

import asyncio
from pathlib import Path

from carbide.infra.config import Config
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def main():
    config = Config.load(workspace=Path.cwd())
    registry = create_default_registry()
    tools = registry.api_schemas()

    agent = Agent(config=config, tool_registry=registry)

    # First query with tools
    print("Query 1: 读取hello.py")
    print("=" * 60)
    async for event in agent.query("读取hello.py", tools=tools):
        if event.type == "chunk" and event.data.type == "content_block_delta":
            if event.data.content:
                print(event.data.content, end="", flush=True)
        elif event.type == "tool_complete":
            print(f"\n[Tool: {event.data['tool_name']}]")

    # Second query - continuation
    print("\n\n" + "=" * 60)
    print("Query 2: 显示结果")
    print("=" * 60)
    has_output = False
    async for event in agent.query("显示结果", tools=tools):
        if event.type == "chunk":
            chunk = event.data
            print(f"[chunk type={chunk.type}]", end=" ")
            if chunk.type == "content_block_delta" and chunk.content:
                print(f"content={chunk.content!r}")
                has_output = True
            else:
                print()
        elif event.type == "tool_complete":
            print(f"[Tool: {event.data['tool_name']}]")
            has_output = True
        elif event.type == "turn_complete":
            print(f"[Turn: {event.data}]")

    if not has_output:
        print("\n⚠️ No output from second query!")


if __name__ == "__main__":
    asyncio.run(main())
