"""End-to-end test simulating the CLI scenario."""

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

    print("Test: 修改hello.py 文件，写一个高级的hello world，可视化")
    print("=" * 60)

    event_count = 0
    text_blocks = []
    tool_calls = []

    async for event in agent.query("修改hello.py 文件，写一个高级的hello world，可视化", tools=tools):
        event_count += 1

        if event.type == "chunk":
            chunk = event.data
            if chunk.type == "content_block_delta":
                if chunk.content:
                    text_blocks.append(chunk.content)
                    print(chunk.content, end="", flush=True)

        elif event.type == "tool_complete":
            tool_calls.append(event.data)
            print(f"\n[Tool: {event.data['tool_name']} - error={event.data['is_error']} - {event.data['duration_ms']:.1f}ms]")

        elif event.type == "turn_complete":
            print(f"\n[Turn complete: {event.data}]")

    print("\n" + "=" * 60)
    print(f"Total events: {event_count}")
    print(f"Text output: {len(''.join(text_blocks))} chars")
    print(f"Tool calls: {len(tool_calls)}")

    for i, tool in enumerate(tool_calls):
        print(f"  {i+1}. {tool['tool_name']}: is_error={tool['is_error']}")


if __name__ == "__main__":
    asyncio.run(main())
