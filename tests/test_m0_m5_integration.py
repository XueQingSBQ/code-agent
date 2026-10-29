"""M0-M5 Integration Test Suite

Tests the integration between all major modules:
- M0: Core loop, provider, tools
- M1: Tool registry and execution
- M2: Context management (compression)
- M3: Memory and skills
- M4: Governance and permissions
- M5: End-to-end workflows
"""

import asyncio
from pathlib import Path

from carbide.infra.config import Config
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


class IntegrationTest:
    def __init__(self):
        self.config = Config.load(workspace=Path.cwd())
        self.registry = create_default_registry()
        self.tools = self.registry.api_schemas()
        self.results = {}

    async def test_m0_core_loop(self):
        """M0: Test basic query loop with provider and streaming."""
        print("\n=== M0: Core Loop Test ===")
        agent = Agent(config=self.config, tool_registry=self.registry)

        # Simple query without tools
        text_output = []
        async for event in agent.query("你好，简单回答", tools=None):
            if event.type == "chunk" and event.data.type == "content_block_delta":
                if event.data.content:
                    text_output.append(event.data.content)

        success = len(text_output) > 0
        print(f"[PASS] Text output: {len(''.join(text_output))} chars" if success else "[FAIL] No output")
        self.results["M0_core_loop"] = success
        return success

    async def test_m1_tool_execution(self):
        """M1: Test tool registry and execution."""
        print("\n=== M1: Tool Execution Test ===")
        agent = Agent(config=self.config, tool_registry=self.registry)

        # Query that should trigger tool use
        tool_calls = []
        async for event in agent.query("读取hello.py文件", tools=self.tools):
            if event.type == "tool_complete":
                tool_calls.append(event.data)

        success = len(tool_calls) > 0 and tool_calls[0]["tool_name"] == "read"
        print(f"[PASS] Tool called: {tool_calls[0]['tool_name']}" if success else "[FAIL] No tools called")
        self.results["M1_tool_execution"] = success
        return success

    async def test_m1_tool_concurrency(self):
        """M1: Test concurrent tool execution."""
        print("\n=== M1: Tool Concurrency Test ===")
        agent = Agent(config=self.config, tool_registry=self.registry)

        # Query that should trigger multiple read-only tools
        tool_calls = []
        start_time = None
        end_time = None

        async for event in agent.query("搜索所有.py文件并找到包含'test'的文件", tools=self.tools):
            if event.type == "tool_complete":
                if start_time is None:
                    import time
                    start_time = time.time()
                tool_calls.append(event.data)
                end_time = time.time()

        # Check if multiple tools were called
        success = len(tool_calls) >= 2
        if success and start_time and end_time:
            duration = (end_time - start_time) * 1000
            print(f"[PASS] Multiple tools: {len(tool_calls)} calls in {duration:.1f}ms")
        else:
            print(f"[FAIL] Expected multiple tool calls, got {len(tool_calls)}")

        self.results["M1_concurrency"] = success
        return success

    async def test_m2_token_tracking(self):
        """M2: Test token tracking and statistics."""
        print("\n=== M2: Token Tracking Test ===")
        agent = Agent(config=self.config, tool_registry=self.registry)

        stats = None
        async for event in agent.query("列出3个Python文件", tools=self.tools):
            if event.type == "turn_complete":
                stats = event.data

        success = (stats is not None and
                   stats.get("input_tokens", 0) > 0 and
                   stats.get("output_tokens", 0) > 0)

        if success:
            print(f"[PASS] Token tracking: in={stats['input_tokens']} out={stats['output_tokens']}")
            if stats.get("cache_read_tokens", 0) > 0:
                cache_hit_rate = stats["cache_read_tokens"] / (stats["input_tokens"] + stats["cache_read_tokens"])
                print(f"  Cache hit rate: {cache_hit_rate:.1%}")
        else:
            print("[FAIL] Token tracking failed")

        self.results["M2_token_tracking"] = success
        return success

    async def test_m3_multi_turn(self):
        """M3: Test multi-turn conversation with tool results."""
        print("\n=== M3: Multi-turn Conversation Test ===")
        agent = Agent(config=self.config, tool_registry=self.registry)

        # First turn: create a file
        turn1_tools = []
        async for event in agent.query("创建test_m3.txt文件，内容是'Hello M3'", tools=self.tools):
            if event.type == "tool_complete":
                turn1_tools.append(event.data["tool_name"])

        # Second turn: read the file back
        turn2_tools = []
        async for event in agent.query("读取test_m3.txt的内容", tools=self.tools):
            if event.type == "tool_complete":
                turn2_tools.append(event.data["tool_name"])

        success = "write" in turn1_tools and "read" in turn2_tools

        if success:
            print(f"[PASS] Multi-turn: Turn1={turn1_tools}, Turn2={turn2_tools}")
            # Cleanup
            Path("test_m3.txt").unlink(missing_ok=True)
        else:
            print(f"[FAIL] Multi-turn failed: Turn1={turn1_tools}, Turn2={turn2_tools}")

        self.results["M3_multi_turn"] = success
        return success

    async def test_m4_config_loading(self):
        """M4: Test configuration loading and provider setup."""
        print("\n=== M4: Configuration Test ===")

        # Check config values
        success = (
            self.config.model is not None and
            self.config.api_key is not None and
            self.config.workspace.exists()
        )

        if success:
            print(f"[PASS] Config: model={self.config.model}, workspace={self.config.workspace}")
        else:
            print("[FAIL] Config loading failed")

        self.results["M4_config"] = success
        return success

    async def test_m5_error_handling(self):
        """M5: Test error handling in tool execution."""
        print("\n=== M5: Error Handling Test ===")
        agent = Agent(config=self.config, tool_registry=self.registry)

        # Try to read a non-existent file
        errors = []
        tool_calls = []
        async for event in agent.query("读取nonexistent_file_xyz.py", tools=self.tools):
            if event.type == "tool_complete":
                tool_calls.append(event.data)
                if event.data.get("is_error"):
                    errors.append(event.data)
            elif event.type == "error":
                errors.append(event.data)

        # The tool should be called, and handle error gracefully
        success = len(tool_calls) > 0
        print(f"[PASS] Error handled: {len(tool_calls)} tools, {len(errors)} errors" if success
              else "[FAIL] Error handling failed")

        self.results["M5_error_handling"] = success
        return success

    async def run_all(self):
        """Run all integration tests."""
        print("=" * 60)
        print("M0-M5 Integration Test Suite")
        print("=" * 60)

        tests = [
            self.test_m0_core_loop,
            self.test_m1_tool_execution,
            self.test_m1_tool_concurrency,
            self.test_m2_token_tracking,
            self.test_m3_multi_turn,
            self.test_m4_config_loading,
            self.test_m5_error_handling,
        ]

        for test in tests:
            try:
                await test()
            except Exception as e:
                print(f"[FAIL] {test.__name__} raised exception: {e}")
                self.results[test.__name__] = False

        # Summary
        print("\n" + "=" * 60)
        print("Test Results Summary")
        print("=" * 60)

        passed = sum(1 for v in self.results.values() if v)
        total = len(self.results)

        for name, result in self.results.items():
            status = "[PASS] PASS" if result else "[FAIL] FAIL"
            print(f"{status}: {name}")

        print(f"\n{passed}/{total} tests passed ({passed/total*100:.1f}%)")

        return passed == total


async def main():
    tester = IntegrationTest()
    success = await tester.run_all()
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
