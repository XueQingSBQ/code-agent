"""Test persistent bash session functionality."""

import asyncio
import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from carbide.tools.bash_session import BashSession


async def test_basic_execution():
    """Test basic command execution."""
    print("\n[TEST] Basic execution...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        output, exit_code = await session.execute("echo 'Hello World'")
        assert exit_code == 0, f"Expected exit code 0, got {exit_code}"
        assert "Hello World" in output, f"Expected 'Hello World' in output, got: {output}"
        print("[PASS] Basic execution")
    finally:
        await session.close()


async def test_state_persistence_cd():
    """Test that cd state persists across commands."""
    print("\n[TEST] State persistence - cd...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        # Create temp directory
        await session.execute("mkdir -p /tmp/carbide_test_dir")

        # Change directory
        output1, code1 = await session.execute("cd /tmp/carbide_test_dir")
        assert code1 == 0, f"cd failed with code {code1}"

        # Verify we're still in that directory
        output2, code2 = await session.execute("pwd")
        assert code2 == 0, f"pwd failed with code {code2}"
        assert "carbide_test_dir" in output2, f"Directory not persisted. pwd output: {output2}"

        # Cleanup
        await session.execute("cd /tmp && rm -rf carbide_test_dir")

        print("[PASS] State persistence - cd")
    finally:
        await session.close()


async def test_state_persistence_env():
    """Test that environment variables persist across commands."""
    print("\n[TEST] State persistence - env vars...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        # Set environment variable
        output1, code1 = await session.execute("export TEST_VAR='carbide_test_value'")
        assert code1 == 0, f"export failed with code {code1}"

        # Verify variable persists
        output2, code2 = await session.execute("echo $TEST_VAR")
        assert code2 == 0, f"echo failed with code {code2}"
        assert "carbide_test_value" in output2, f"Env var not persisted. Output: {output2}"

        print("[PASS] State persistence - env vars")
    finally:
        await session.close()


async def test_state_persistence_function():
    """Test that shell functions persist across commands."""
    print("\n[TEST] State persistence - functions...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        # Define function
        output1, code1 = await session.execute("test_func() { echo 'function works'; }")
        assert code1 == 0, f"function definition failed with code {code1}"

        # Call function
        output2, code2 = await session.execute("test_func")
        assert code2 == 0, f"function call failed with code {code2}"
        assert "function works" in output2, f"Function not persisted. Output: {output2}"

        print("[PASS] State persistence - functions")
    finally:
        await session.close()


async def test_exit_code():
    """Test that exit codes are correctly captured."""
    print("\n[TEST] Exit code capture...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        # Successful command
        output1, code1 = await session.execute("true")
        assert code1 == 0, f"Expected exit code 0 for true, got {code1}"

        # Failed command
        output2, code2 = await session.execute("false")
        assert code2 == 1, f"Expected exit code 1 for false, got {code2}"

        # Custom exit code using (exit N)
        output3, code3 = await session.execute("(exit 42)")
        assert code3 == 42, f"Expected exit code 42, got {code3}"

        print("[PASS] Exit code capture")
    finally:
        await session.close()


async def test_multiple_commands():
    """Test executing multiple commands in sequence."""
    print("\n[TEST] Multiple commands...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        commands = [
            ("echo 'first'", 0, "first"),
            ("echo 'second'", 0, "second"),
            ("echo 'third'", 0, "third"),
        ]

        for cmd, expected_code, expected_output in commands:
            output, code = await session.execute(cmd)
            assert code == expected_code, f"Command '{cmd}' failed with code {code}"
            assert expected_output in output, f"Expected '{expected_output}' in output: {output}"

        print("[PASS] Multiple commands")
    finally:
        await session.close()


async def test_output_without_trailing_newline():
    """A command without a final newline must not swallow the protocol marker."""
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        output, code = await session.execute("printf hello")
        assert code == 0
        assert output == "hello"
    finally:
        await session.close()


async def test_exit_command_reports_status_and_keeps_session_alive():
    """An explicit exit is isolated so the persistent session remains usable."""
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        output, code = await session.execute("echo before; exit 3")
        assert code == 3
        assert output == "before"

        output, code = await session.execute("echo after")
        assert code == 0
        assert output == "after"
    finally:
        await session.close()


async def test_timeout():
    """Test command timeout handling."""
    print("\n[TEST] Timeout handling...")
    session = BashSession(workspace=os.getcwd(), shell_backend="bash")

    try:
        # Command that sleeps for 5 seconds with 1 second timeout
        try:
            output, code = await session.execute("sleep 5", timeout=1.0)
            print("[FAIL] Timeout handling - should have raised TimeoutError")
            await session.close()
            return
        except TimeoutError:
            print("[PASS] Timeout handling")
    finally:
        # Session should be closed after timeout
        pass


async def main():
    print("=" * 60)
    print("Testing Persistent BashSession (M1 Core Deliverable)")
    print("=" * 60)

    tests = [
        test_basic_execution,
        test_state_persistence_cd,
        test_state_persistence_env,
        test_state_persistence_function,
        test_exit_code,
        test_multiple_commands,
        test_timeout,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
