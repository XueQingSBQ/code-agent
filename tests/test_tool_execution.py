"""Test tool execution in CLI."""

import asyncio
from pathlib import Path

from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def main():
    # Create agent with tool registry
    registry = create_default_registry()
    tools = registry.api_schemas()

    agent = Agent(
        project_dir=Path.cwd(),
        tool_registry=registry,
    )

    print("Testing tool execution...")
    print("Query: 创建一个hello.py文件，内容是print('Hello, World!')")
    print("=" * 60)

    async for event in agent.query(
        "创建一个hello.py文件，内容是print('Hello, World!')",
        tools=tools,
    ):
        if event.type == "chunk" and event.data:
            if event.data.type == "content_block_delta" and event.data.content:
                print(event.data.content, end="", flush=True)
        elif event.type == "tool_complete":
            print(f"\n[Tool executed: {event.data['tool_name']}]")
        elif event.type == "turn_complete":
            print(f"\n\n[Turn complete: {event.data}]")
        elif event.type == "error":
            print(f"\n[Error: {event.data}]")

    # Check if file was created
    if Path("hello.py").exists():
        print("\n✓ File hello.py was created successfully!")
        print("Content:")
        print(Path("hello.py").read_text())
    else:
        print("\n✗ File hello.py was NOT created")

    print("\nAgent stats:")
    print(agent.get_stats())


if __name__ == "__main__":
    asyncio.run(main())
