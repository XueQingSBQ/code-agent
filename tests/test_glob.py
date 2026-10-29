"""Test glob tool directly."""

import asyncio
from pathlib import Path

from carbide.tools.base import ToolContext
from carbide.tools.search import GlobParams, GlobTool


async def main():
    tool = GlobTool()
    ctx = ToolContext(workspace=Path.cwd())

    # Test with simple pattern
    print("Test 1: *.py")
    args = GlobParams(pattern="*.py")
    result = await tool.run(args, ctx)
    print(f"Success: {not result.is_error}")
    print(f"Content: {result.content[:200] if result.content else 'None'}")
    if result.is_error:
        print(f"Error: {result.error}")

    print("\n" + "="*60 + "\n")

    # Test with pattern that might fail
    print("Test 2: hello.py")
    args = GlobParams(pattern="hello.py")
    result = await tool.run(args, ctx)
    print(f"Success: {not result.is_error}")
    print(f"Content: {result.content}")
    if result.is_error:
        print(f"Error: {result.error}")

if __name__ == "__main__":
    asyncio.run(main())
