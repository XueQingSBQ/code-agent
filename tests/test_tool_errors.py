"""Debug tool execution errors."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def test_tool_errors():
    """Debug why tools are failing."""

    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create test file
        test_file = project_dir / "test.txt"
        test_file.write_text("Hello World")

        print("Debug: Tool Execution Errors")
        print("=" * 70)

        registry = create_default_registry()
        tools = registry.api_schemas()

        agent = Agent(
            project_dir=project_dir,
            enable_governance=False,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
            enable_session_store=False,
            tool_registry=registry,
        )

        print(f"\nTest file: {test_file}")
        print(f"Exists: {test_file.exists()}")
        print(f"Content: {test_file.read_text()}")

        # Check message history to see tool results
        print(f"\nSending query...")

        async for event in agent.query(
            f"Read the file {test_file}",
            tools=tools
        ):
            if event.type == "tool_complete":
                print(f"\n[TOOL_COMPLETE]")
                print(f"  Tool: {event.data.get('tool_name')}")
                print(f"  Error: {event.data.get('is_error')}")
                print(f"  Duration: {event.data.get('duration_ms')}ms")

            elif event.type == "turn_complete":
                break

        # Check actual messages to see tool results
        print(f"\n\nMessage History:")
        print("=" * 70)

        state = agent.loop.get_state()
        for i, msg in enumerate(state.messages):
            print(f"\nMessage {i+1}:")
            print(f"  Role: {msg['role']}")

            content = msg.get('content', [])
            if isinstance(content, str):
                print(f"  Content: {content[:100]}...")
            elif isinstance(content, list):
                print(f"  Content blocks: {len(content)}")
                for j, block in enumerate(content):
                    if isinstance(block, dict):
                        block_type = block.get('type', 'unknown')
                        print(f"    Block {j+1}: type={block_type}")

                        if block_type == 'tool_result':
                            print(f"      tool_use_id: {block.get('tool_use_id')}")
                            print(f"      is_error: {block.get('is_error')}")
                            result_content = block.get('content', '')
                            if isinstance(result_content, str):
                                print(f"      content: {result_content[:200]}")
                            elif isinstance(result_content, list):
                                for item in result_content:
                                    if isinstance(item, dict):
                                        print(f"      content type: {item.get('type')}")
                                        if item.get('type') == 'text':
                                            print(f"      text: {item.get('text', '')[:200]}")


if __name__ == "__main__":
    asyncio.run(test_tool_errors())
