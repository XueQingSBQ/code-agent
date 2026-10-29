"""Test why model returns no text output."""

import asyncio
from pathlib import Path

from carbide.infra.config import Config
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def main():
    config = Config.load(workspace=Path.cwd())
    registry = create_default_registry()
    tools = registry.api_schemas()

    print(f"Config: model={config.model}, base_url={config.base_url}")
    print(f"Tools: {len(tools)} available")
    print("=" * 60)

    agent = Agent(config=config, tool_registry=registry)

    print("Query: 你好")
    print("=" * 60)

    text_output = []
    async for event in agent.query("你好", tools=tools):
        if event.type == "chunk":
            chunk = event.data
            if chunk.type == "content_block_delta" and chunk.content:
                text_output.append(chunk.content)
                print(chunk.content, end="", flush=True)
        elif event.type == "tool_complete":
            print(f"\n[Tool: {event.data['tool_name']}]")
        elif event.type == "turn_complete":
            print(f"\n\n[Turn stats: {event.data}]")

    print(f"\n\nTotal text output: {len(''.join(text_output))} chars")
    if not text_output:
        print("⚠️ No text output from model!")


if __name__ == "__main__":
    asyncio.run(main())
