"""Test BashTool with persistent session integration."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from carbide.tools.shell import BashTool, BashParams
from carbide.tools.base import ToolContext


async def test_bashtool_persistence():
    """Test that BashTool maintains state across multiple invocations."""
    print("\n[TEST] BashTool state persistence...")

    tool = BashTool()
    ctx = ToolContext(
        workspace=os.getcwd(),
        shell="bash",
    )

    # Test 1: Set environment variable
    result1 = await tool.run(BashParams(command="export TEST_VAR='persistence_test'"), ctx)
    assert not result1.is_error, f"export failed: {result1.content}"
    print("  [PASS] Set environment variable")

    # Test 2: Read environment variable (should persist)
    result2 = await tool.run(BashParams(command="echo $TEST_VAR"), ctx)
    assert not result2.is_error, f"echo failed: {result2.content}"
    assert "persistence_test" in result2.content, f"Env var not persisted: {result2.content}"
    print("  [PASS] Environment variable persisted")

    # Test 3: Change directory
    result3 = await tool.run(BashParams(command="cd /tmp"), ctx)
    assert not result3.is_error, f"cd failed: {result3.content}"
    print("  [PASS] Changed directory")

    # Test 4: Verify still in that directory
    result4 = await tool.run(BashParams(command="pwd"), ctx)
    assert not result4.is_error, f"pwd failed: {result4.content}"
    assert "/tmp" in result4.content or "\\tmp" in result4.content, f"Directory not persisted: {result4.content}"
    print("  [PASS] Directory persisted")

    # Test 5: Define function
    result5 = await tool.run(BashParams(command="test_func() { echo 'function_output'; }"), ctx)
    assert not result5.is_error, f"function definition failed: {result5.content}"
    print("  [PASS] Defined function")

    # Test 6: Call function
    result6 = await tool.run(BashParams(command="test_func"), ctx)
    assert not result6.is_error, f"function call failed: {result6.content}"
    assert "function_output" in result6.content, f"Function not persisted: {result6.content}"
    print("  [PASS] Function persisted")

    print("[PASS] BashTool state persistence")


async def test_bashtool_isolation():
    """Test that different workspaces have isolated sessions."""
    print("\n[TEST] BashTool session isolation...")

    tool = BashTool()

    # Use current directory and a subdirectory for isolation test
    workspace1 = os.getcwd()
    workspace2 = os.path.join(os.getcwd(), "src")  # Use existing src directory

    # Create two contexts with different workspaces
    ctx1 = ToolContext(workspace=workspace1, shell="bash")
    ctx2 = ToolContext(workspace=workspace2, shell="bash")

    # Set different env vars in each
    result1 = await tool.run(BashParams(command="export WORKSPACE='one'"), ctx1)
    assert not result1.is_error, f"Failed to set WORKSPACE in ctx1: {result1.content}"

    result2 = await tool.run(BashParams(command="export WORKSPACE='two'"), ctx2)
    assert not result2.is_error, f"Failed to set WORKSPACE in ctx2: {result2.content}"

    # Verify isolation
    result3 = await tool.run(BashParams(command="echo $WORKSPACE"), ctx1)
    assert "one" in result3.content, f"Expected 'one', got: {result3.content}"

    result4 = await tool.run(BashParams(command="echo $WORKSPACE"), ctx2)
    assert "two" in result4.content, f"Expected 'two', got: {result4.content}"

    print("[PASS] BashTool session isolation")


async def test_bashtool_error_handling():
    """Test error handling in BashTool."""
    print("\n[TEST] BashTool error handling...")

    tool = BashTool()
    ctx = ToolContext(workspace=os.getcwd(), shell="bash")

    # Test command that fails
    result = await tool.run(BashParams(command="false"), ctx)
    assert result.is_error, "Expected error for 'false' command"
    assert "exit 1" in result.content, f"Expected exit code in error: {result.content}"

    print("[PASS] BashTool error handling")


async def main():
    print("=" * 60)
    print("Testing BashTool with Persistent Session (M1)")
    print("=" * 60)

    tests = [
        test_bashtool_persistence,
        test_bashtool_isolation,
        test_bashtool_error_handling,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
