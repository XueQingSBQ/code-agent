"""Test CLI integration with all subsystems."""

import subprocess
import sys
import tempfile
from pathlib import Path


def run_cli_basic():
    """Test basic CLI functionality."""

    print("CLI Integration Test")
    print("=" * 70)

    # Test 1: CLI help
    print("\nTest 1: CLI --help")
    result = subprocess.run(
        [sys.executable, "-m", "carbide.cli.app", "--help"],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print("  PASS - CLI help works")
        print(f"  Output preview: {result.stdout[:100]}...")
    else:
        print(f"  FAIL - Exit code: {result.returncode}")
        print(f"  Error: {result.stderr}")
        return False

    # Test 2: CLI version
    print("\nTest 2: CLI --version")
    result = subprocess.run(
        [sys.executable, "-m", "carbide.cli.app", "--version"],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(f"  PASS - Version: {result.stdout.strip()}")
    else:
        print(f"  WARN - Version check failed")

    # Test 3: One-shot mode with simple task
    print("\nTest 3: One-shot mode")

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        test_file = workspace / "test.txt"
        test_file.write_text("Hello CLI")

        print(f"  Workspace: {workspace}")
        print(f"  Test file: {test_file}")

        result = subprocess.run(
            [sys.executable, "-m", "carbide.cli.app", "-C", str(workspace), "What is in test.txt?"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        print(f"  Exit code: {result.returncode}")
        print(f"  Output length: {len(result.stdout)} chars")

        if result.returncode == 0:
            print("  PASS - CLI executed")
            if "Hello CLI" in result.stdout or "test.txt" in result.stdout:
                print("  PASS - Response mentions file")
            else:
                print("  WARN - Response may not include file content")
            print(f"  Output preview: {result.stdout[:200]}...")
        else:
            print(f"  FAIL - CLI error")
            print(f"  Stderr: {result.stderr[:500]}")
            return False

    print("\n" + "=" * 70)
    print("CLI Integration Tests: COMPLETE")
    return True


if __name__ == "__main__":
    try:
        success = run_cli_basic()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nEXCEPTION: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
