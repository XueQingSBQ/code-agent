"""Debug glob tool failure in CLI context."""

import asyncio
from pathlib import Path

from carbide.engine.executor import StreamingToolExecutor
from carbide.infra.config import Config
from carbide.providers.base import StreamToolUse
from carbide.tools.base import ToolContext
from carbide.tools.defaults import create_default_registry


async def main():
    config = Config.load(workspace=Path.cwd())
    registry = create_default_registry()
    ctx = ToolContext(workspace=Path.cwd())

    # Create executor
    executor = StreamingToolExecutor(registry, ctx)

    # Simulate glob tool call
    glob_call = StreamToolUse(
        id="test_glob_1",
        name="glob",
        arguments={"pattern": "*.py"}
    )

    print("Adding glob tool...")
    executor.add_tool(glob_call)

    print("Gathering results...")
    completed = await executor.gather_remaining()

    for result in completed:
        print(f"\nTool: {result.call.name}")
        print(f"Duration: {result.duration_ms}ms")
        print(f"Is Error: {result.result.is_error}")
        print(f"Content preview: {result.result.content[:100] if result.result.content else 'None'}")
        if result.result.is_error:
            print(f"Error: {result.result.error}")


asyncio.run(main())
