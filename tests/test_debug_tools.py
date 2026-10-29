"""Debug test: Minimal tool calling test."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def test_minimal_tool_call():
    """Minimal test to debug tool calling."""

    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create a test file
        test_file = project_dir / "test.txt"
        test_file.write_text("Hello from test file!")

        print("Debug: Minimal Tool Calling Test")
        print("=" * 70)

        # Create tool registry
        registry = create_default_registry()
        tools = registry.api_schemas()

        print(f"\n1. Registry Info:")
        print(f"   Tools count: {len(registry)}")
        print(f"   Tools: {[t.name for t in registry.all()]}")
        print(f"   Schemas count: {len(tools)}")

        # Create agent
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

        print(f"\n2. Agent Info:")
        print(f"   Provider: {agent.provider}")
        print(f"   Loop.tool_registry: {agent.loop.tool_registry}")
        print(f"   Loop has {len(agent.loop.tool_registry) if agent.loop.tool_registry else 0} tools")

        # Test 1: Very explicit prompt
        print(f"\n3. Test with explicit file path:")
        print(f"   File: {test_file}")
        print(f"   Exists: {test_file.exists()}")
        print(f"   Content: {test_file.read_text()}")

        print(f"\n4. Sending query...")

        events_received = []
        response_text = ""

        async for event in agent.query(
            f"Use the read tool to read the file: {test_file}",
            tools=tools
        ):
            events_received.append(event.type)

            if event.type == "chunk" and event.data:
                if hasattr(event.data, "content") and event.data.content:
                    response_text += event.data.content

            elif event.type == "tool_use":
                print(f"   [TOOL_USE] event received!")
                print(f"     Tool: {event.data.get('name', 'unknown')}")
                print(f"     Input: {event.data.get('input', {})}")

            elif event.type == "tool_result":
                print(f"   [TOOL_RESULT] event received!")
                print(f"     Content: {str(event.data.get('content', ''))[:100]}...")

            elif event.type == "tool_complete":
                print(f"   [TOOL_COMPLETE] event received!")

            elif event.type == "error":
                print(f"   [ERROR]: {event.data}")

            elif event.type == "turn_complete":
                print(f"   [TURN_COMPLETE]")
                break

        print(f"\n5. Results:")
        print(f"   Events: {set(events_received)}")
        print(f"   Response: {response_text[:200]}...")

        # Check if tools were called
        if "tool_use" in events_received:
            print(f"\n   SUCCESS: Tools were called!")
        else:
            print(f"\n   FAILURE: No tools were called")
            print(f"\n   Possible reasons:")
            print(f"   - Model chose not to use tools")
            print(f"   - Tools not properly passed to provider")
            print(f"   - Provider/API issue")

        print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(test_minimal_tool_call())
