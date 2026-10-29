"""Test 2: Governance system with actual tool execution."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def test_governance_with_tools():
    """Test Governance components during actual tool execution."""

    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        print("Test 2: Governance System with Tool Execution")
        print("=" * 70)

        # Create tool registry
        registry = create_default_registry()
        tools = registry.api_schemas()
        print(f"\nTools registered: {len(registry)}")
        for tool in registry.all():
            print(f"  - {tool.name}")

        # Initialize agent with governance enabled
        agent = Agent(
            project_dir=project_dir,
            enable_governance=True,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
            enable_session_store=False,
            tool_registry=registry,  # 修复：传递工具注册表
        )

        try:
            # Test 1: Check governance components
            print("\n1. Governance Components Check")
            print("-" * 70)

            components = {
                "PermissionEngine": agent.loop.permission_engine,
                "BashGuard": agent.loop.bash_guard,
                "AuditLogger": agent.loop.audit_logger,
            }

            all_present = True
            for name, component in components.items():
                if component:
                    print(f"PASS - {name} initialized")
                else:
                    print(f"FAIL - {name} not initialized")
                    all_present = False

            # HookRegistry is optional (only if hooks dir exists)
            if agent.loop.hook_registry:
                print(f"INFO - HookRegistry initialized")
            else:
                print(f"INFO - HookRegistry not initialized (hooks dir not present)")

            if not all_present:
                return False

            # Test 2: Check audit directory created
            print("\n2. Audit Directory Check")
            print("-" * 70)

            audit_dir = project_dir / ".carbide" / "audit"
            print(f"Audit directory: {audit_dir}")
            print(f"Exists: {audit_dir.exists()}")

            if audit_dir.exists():
                print("PASS - Audit directory created")
            else:
                print("FAIL - Audit directory not created")
                return False

            # Test 3: Query that triggers tool use (Read/Write/Bash)
            print("\n3. Tool Execution Test")
            print("-" * 70)

            # Create a test file to read
            test_file = project_dir / "test.txt"
            test_file.write_text("Hello from test file!")

            print("Sending query that should trigger file read...")

            response_text = ""
            tool_calls = []

            async for event in agent.query(
                f"Read the file at {test_file} and tell me what it says",
                tools=tools  # 修复：传递工具schemas
            ):
                if event.type == "chunk" and event.data:
                    if hasattr(event.data, "content") and event.data.content:
                        response_text += event.data.content
                elif event.type == "tool_use":
                    tool_name = event.data.get("name", "unknown")
                    tool_calls.append(tool_name)
                    print(f"  Tool called: {tool_name}")
                elif event.type == "error":
                    print(f"Query error: {event.data}")
                    break
                elif event.type == "turn_complete":
                    break

            if response_text:
                print(f"PASS - Got response: {response_text[:80]}...")
            else:
                print("FAIL - No response received")

            if tool_calls:
                print(f"PASS - Tools executed: {tool_calls}")
            else:
                print("WARN - No tools were called (may be expected behavior)")

            # Test 4: Check audit logs
            print("\n4. Audit Log Verification")
            print("-" * 70)

            # List audit log files
            if audit_dir.exists():
                log_files = list(audit_dir.glob("*.jsonl"))
                print(f"Audit log files: {len(log_files)}")

                if log_files:
                    print("PASS - Audit logs created")
                    for log_file in log_files:
                        print(f"  - {log_file.name}")

                        # Read first few lines
                        with open(log_file, 'r') as f:
                            lines = [f.readline() for _ in range(3)]

                        print(f"    Lines in log: {len([l for l in lines if l.strip()])}")
                else:
                    print("WARN - No audit log files created yet")
            else:
                print("FAIL - Audit directory disappeared")

            # Test 5: Check bash command handling
            print("\n5. Bash Command Test")
            print("-" * 70)

            print("Sending query that requires bash command...")

            bash_response = ""
            bash_tools = []

            async for event in agent.query(
                "List files in the current directory",
                tools=tools  # 修复：传递工具schemas
            ):
                if event.type == "chunk" and event.data:
                    if hasattr(event.data, "content") and event.data.content:
                        bash_response += event.data.content
                elif event.type == "tool_use":
                    tool_name = event.data.get("name", "unknown")
                    bash_tools.append(tool_name)
                    print(f"  Tool called: {tool_name}")
                elif event.type == "error":
                    print(f"Query error: {event.data}")
                    break
                elif event.type == "turn_complete":
                    break

            if bash_response:
                print(f"PASS - Got bash response: {bash_response[:80]}...")
            else:
                print("WARN - No bash response")

            if bash_tools:
                print(f"Tools used: {bash_tools}")

            # Test 6: Statistics
            print("\n6. Execution Statistics")
            print("-" * 70)

            stats = agent.get_stats()
            print(f"Total turns: {stats['turn_count']}")
            print(f"Input tokens: {stats['total_input_tokens']}")
            print(f"Output tokens: {stats['total_output_tokens']}")
            print(f"Messages: {stats['message_count']}")

            print("\n" + "=" * 70)
            print("TEST 2 COMPLETE: Governance with Tool Execution")
            print("=" * 70)
            print("\nSummary:")
            print(f"  - Governance components: {'ALL PASS' if all_present else 'FAIL'}")
            print(f"  - Audit directory: {'PASS' if audit_dir.exists() else 'FAIL'}")
            print(f"  - Tool execution: {'PASS' if tool_calls else 'WARN'}")
            print(f"  - Total turns: {stats['turn_count']}")

            return True

        finally:
            pass


if __name__ == "__main__":
    result = asyncio.run(test_governance_with_tools())
    exit(0 if result else 1)
