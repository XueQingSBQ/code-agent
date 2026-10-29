"""Comprehensive Real API Test Suite

Tests all modules with real API calls to find bugs and integration issues.
"""

import asyncio
import sys
from pathlib import Path

from carbide.infra.config import Config
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


class RealAPITest:
    def __init__(self):
        self.config = Config.load(workspace=Path.cwd())
        self.registry = create_default_registry()
        self.tools = self.registry.api_schemas()
        self.bugs_found = []
        self.test_results = {}

    def log_bug(self, test_name: str, description: str, error: Exception | None = None):
        """Log a bug found during testing."""
        bug = {
            "test": test_name,
            "description": description,
            "error": str(error) if error else None
        }
        self.bugs_found.append(bug)
        print(f"[BUG] {test_name}: {description}")
        if error:
            print(f"      Error: {error}")

    async def test_basic_query(self):
        """Test basic query without tools."""
        print("\n=== Test: Basic Query ===")
        try:
            agent = Agent(config=self.config, tool_registry=self.registry)

            response_chunks = []
            async for event in agent.query("Say 'test' in one word"):
                if event.type == "chunk" and event.data.type == "content_block_delta":
                    if event.data.content:
                        response_chunks.append(event.data.content)
                elif event.type == "error":
                    self.log_bug("basic_query", f"Query error: {event.data}")
                    self.test_results["basic_query"] = False
                    return False

            response = "".join(response_chunks)
            if not response:
                self.log_bug("basic_query", "No response from model")
                self.test_results["basic_query"] = False
                return False

            print(f"[PASS] Got response: {response[:50]}...")
            self.test_results["basic_query"] = True
            return True

        except Exception as e:
            self.log_bug("basic_query", "Exception during basic query", e)
            self.test_results["basic_query"] = False
            return False

    async def test_tool_execution(self):
        """Test single tool execution."""
        print("\n=== Test: Tool Execution ===")
        try:
            agent = Agent(config=self.config, tool_registry=self.registry)

            # Create a test file first
            test_file = Path("test_api_file.txt")
            test_file.write_text("Test content for API testing")

            tool_called = False
            tool_error = False

            async for event in agent.query("Read the file test_api_file.txt", tools=self.tools):
                if event.type == "tool_complete":
                    tool_called = True
                    if event.data.get("is_error"):
                        tool_error = True
                        self.log_bug("tool_execution", f"Tool error: {event.data}")
                elif event.type == "error":
                    self.log_bug("tool_execution", f"Query error: {event.data}")
                    test_file.unlink(missing_ok=True)
                    self.test_results["tool_execution"] = False
                    return False

            test_file.unlink(missing_ok=True)

            if not tool_called:
                self.log_bug("tool_execution", "Tool was not called")
                self.test_results["tool_execution"] = False
                return False

            if tool_error:
                self.log_bug("tool_execution", "Tool execution failed")
                self.test_results["tool_execution"] = False
                return False

            print("[PASS] Tool executed successfully")
            self.test_results["tool_execution"] = True
            return True

        except Exception as e:
            self.log_bug("tool_execution", "Exception during tool execution", e)
            self.test_results["tool_execution"] = False
            return False

    async def test_multi_turn(self):
        """Test multi-turn conversation with state."""
        print("\n=== Test: Multi-turn Conversation ===")
        try:
            agent = Agent(config=self.config, tool_registry=self.registry)

            # Turn 1: Create file
            turn1_tools = []
            async for event in agent.query("Create file test_multi.txt with content 'Turn 1'", tools=self.tools):
                if event.type == "tool_complete":
                    turn1_tools.append(event.data["tool_name"])
                elif event.type == "error":
                    self.log_bug("multi_turn", f"Turn 1 error: {event.data}")
                    self.test_results["multi_turn"] = False
                    return False

            if "write" not in turn1_tools:
                self.log_bug("multi_turn", "Turn 1 did not write file")
                self.test_results["multi_turn"] = False
                return False

            # Turn 2: Read file
            turn2_tools = []
            async for event in agent.query("Now read test_multi.txt", tools=self.tools):
                if event.type == "tool_complete":
                    turn2_tools.append(event.data["tool_name"])
                elif event.type == "error":
                    self.log_bug("multi_turn", f"Turn 2 error: {event.data}")
                    Path("test_multi.txt").unlink(missing_ok=True)
                    self.test_results["multi_turn"] = False
                    return False

            Path("test_multi.txt").unlink(missing_ok=True)

            if "read" not in turn2_tools:
                self.log_bug("multi_turn", "Turn 2 did not read file")
                self.test_results["multi_turn"] = False
                return False

            print("[PASS] Multi-turn conversation works")
            self.test_results["multi_turn"] = True
            return True

        except Exception as e:
            self.log_bug("multi_turn", "Exception during multi-turn test", e)
            self.test_results["multi_turn"] = False
            return False

    async def test_concurrent_tools(self):
        """Test concurrent tool execution."""
        print("\n=== Test: Concurrent Tool Execution ===")
        try:
            agent = Agent(config=self.config, tool_registry=self.registry)

            # Create test files
            for i in range(3):
                Path(f"test_concurrent_{i}.txt").write_text(f"Content {i}")

            tools_called = []
            async for event in agent.query("Read all test_concurrent_*.txt files", tools=self.tools):
                if event.type == "tool_complete":
                    tools_called.append({
                        "name": event.data["tool_name"],
                        "duration": event.data["duration_ms"],
                        "error": event.data.get("is_error", False)
                    })
                elif event.type == "error":
                    self.log_bug("concurrent_tools", f"Query error: {event.data}")
                    for i in range(3):
                        Path(f"test_concurrent_{i}.txt").unlink(missing_ok=True)
                    self.test_results["concurrent_tools"] = False
                    return False

            # Cleanup
            for i in range(3):
                Path(f"test_concurrent_{i}.txt").unlink(missing_ok=True)

            if len(tools_called) < 2:
                self.log_bug("concurrent_tools", f"Expected multiple tools, got {len(tools_called)}")
                self.test_results["concurrent_tools"] = False
                return False

            errors = [t for t in tools_called if t["error"]]
            if errors:
                self.log_bug("concurrent_tools", f"Tool errors: {errors}")
                self.test_results["concurrent_tools"] = False
                return False

            print(f"[PASS] Concurrent execution: {len(tools_called)} tools")
            self.test_results["concurrent_tools"] = True
            return True

        except Exception as e:
            self.log_bug("concurrent_tools", "Exception during concurrent test", e)
            self.test_results["concurrent_tools"] = False
            return False

    async def test_token_tracking(self):
        """Test token tracking and caching."""
        print("\n=== Test: Token Tracking ===")
        try:
            agent = Agent(config=self.config, tool_registry=self.registry)

            stats = None
            async for event in agent.query("List 5 Python files", tools=self.tools):
                if event.type == "turn_complete":
                    stats = event.data
                elif event.type == "error":
                    self.log_bug("token_tracking", f"Query error: {event.data}")
                    self.test_results["token_tracking"] = False
                    return False

            if not stats:
                self.log_bug("token_tracking", "No stats received")
                self.test_results["token_tracking"] = False
                return False

            required_fields = ["input_tokens", "output_tokens", "cache_read_tokens"]
            missing = [f for f in required_fields if f not in stats]
            if missing:
                self.log_bug("token_tracking", f"Missing stats fields: {missing}")
                self.test_results["token_tracking"] = False
                return False

            if stats["input_tokens"] == 0:
                self.log_bug("token_tracking", "Input tokens is 0")
                self.test_results["token_tracking"] = False
                return False

            print(f"[PASS] Token tracking: in={stats['input_tokens']} out={stats['output_tokens']}")
            self.test_results["token_tracking"] = True
            return True

        except Exception as e:
            self.log_bug("token_tracking", "Exception during token tracking test", e)
            self.test_results["token_tracking"] = False
            return False

    async def test_error_handling(self):
        """Test error handling for invalid operations."""
        print("\n=== Test: Error Handling ===")
        try:
            agent = Agent(config=self.config, tool_registry=self.registry)

            # Try to read non-existent file
            tool_results = []
            async for event in agent.query("Read nonexistent_xyz_file.txt", tools=self.tools):
                if event.type == "tool_complete":
                    tool_results.append(event.data)
                elif event.type == "error":
                    # This might be expected
                    pass

            if not tool_results:
                self.log_bug("error_handling", "No tool was called for error case")
                self.test_results["error_handling"] = False
                return False

            # Check that error was handled gracefully (not a crash)
            print("[PASS] Error handling works")
            self.test_results["error_handling"] = True
            return True

        except Exception as e:
            self.log_bug("error_handling", "Exception during error handling test", e)
            self.test_results["error_handling"] = False
            return False

    async def test_long_output(self):
        """Test handling of long tool output."""
        print("\n=== Test: Long Output Handling ===")
        try:
            # Create file with long content
            long_content = "Line\n" * 3000
            Path("test_long.txt").write_text(long_content)

            agent = Agent(config=self.config, tool_registry=self.registry)

            completed = False
            async for event in agent.query("Read test_long.txt", tools=self.tools):
                if event.type == "turn_complete":
                    completed = True
                elif event.type == "error":
                    self.log_bug("long_output", f"Error with long output: {event.data}")
                    Path("test_long.txt").unlink(missing_ok=True)
                    self.test_results["long_output"] = False
                    return False

            Path("test_long.txt").unlink(missing_ok=True)

            if not completed:
                self.log_bug("long_output", "Query did not complete")
                self.test_results["long_output"] = False
                return False

            print("[PASS] Long output handled")
            self.test_results["long_output"] = True
            return True

        except Exception as e:
            self.log_bug("long_output", "Exception during long output test", e)
            Path("test_long.txt").unlink(missing_ok=True)
            self.test_results["long_output"] = False
            return False

    async def run_all(self):
        """Run all tests."""
        print("=" * 60)
        print("Real API Test Suite - Bug Detection")
        print("=" * 60)
        print(f"Model: {self.config.model}")
        print(f"Base URL: {self.config.base_url}")
        print("=" * 60)

        tests = [
            self.test_basic_query,
            self.test_tool_execution,
            self.test_multi_turn,
            self.test_concurrent_tools,
            self.test_token_tracking,
            self.test_error_handling,
            self.test_long_output,
        ]

        for test in tests:
            try:
                await test()
            except KeyboardInterrupt:
                print("\n[INTERRUPTED] Test suite interrupted")
                break
            except Exception as e:
                self.log_bug(test.__name__, "Unexpected exception", e)

        # Report
        print("\n" + "=" * 60)
        print("Test Results")
        print("=" * 60)

        passed = sum(1 for v in self.test_results.values() if v)
        total = len(self.test_results)

        for name, result in self.test_results.items():
            status = "[PASS]" if result else "[FAIL]"
            print(f"{status} {name}")

        print(f"\nTests: {passed}/{total} passed ({passed/total*100:.1f}%)")

        if self.bugs_found:
            print(f"\n{'=' * 60}")
            print(f"Bugs Found: {len(self.bugs_found)}")
            print("=" * 60)
            for i, bug in enumerate(self.bugs_found, 1):
                print(f"\n{i}. {bug['test']}")
                print(f"   {bug['description']}")
                if bug['error']:
                    print(f"   Error: {bug['error']}")
        else:
            print("\n[SUCCESS] No bugs found!")

        return len(self.bugs_found) == 0


async def main():
    tester = RealAPITest()
    success = await tester.run_all()
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
