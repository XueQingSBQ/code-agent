"""Test CLI is fully working."""

import subprocess
import sys

def test_cli():
    """Test CLI one-shot mode."""

    print("Testing Carbide CLI...")
    print("=" * 60)

    # Test simple query
    result = subprocess.run(
        [sys.executable, "-m", "carbide", "What is 3+5?"],
        capture_output=True,
        text=True,
        cwd="E:/claude",
        encoding="utf-8",
    )

    print("Query: What is 3+5?")
    print("-" * 60)
    print(result.stdout)

    if result.returncode == 0:
        print("\n[OK] CLI test passed!")
    else:
        print(f"\n[FAIL] CLI returned error code {result.returncode}")
        if result.stderr:
            print("Error:", result.stderr)

if __name__ == "__main__":
    test_cli()
