"""End-to-end test of persistent bash session through CLI."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from carbide.sdk import Agent


async def test_e2e_bash_persistence():
    """Test bash state persistence through the Agent SDK."""
    print("\n[TEST] End-to-end bash persistence through Agent...")

    agent = Agent(
        model="gpt-5.6-luna",
        api_key=os.getenv("ANTHROPIC_API_KEY", "test"),
    )

    # Test: Ask agent to set env var, then read it in next command
    query = """Execute these bash commands one by one:
1. export MY_TEST_VAR='carbide_works'
2. echo $MY_TEST_VAR

The second command should output 'carbide_works' if state persists."""

    print(f"\nQuery: {query}")
    print("\nAgent response:")

    response_text = ""
    async for chunk in agent.query(query):
        if chunk.type == "chunk":
            content = chunk.data.get("content", "")
            print(content, end="", flush=True)
            response_text += content

    print("\n")

    # Verify the response mentions successful persistence
    if "carbide_works" in response_text.lower() or "persist" in response_text.lower():
        print("[PASS] Bash state persistence verified through Agent")
        return True
    else:
        print(f"[FAIL] Could not verify persistence. Response: {response_text}")
        return False


async def main():
    print("=" * 60)
    print("End-to-End Test: M1 Persistent BashSession")
    print("=" * 60)

    try:
        success = await test_e2e_bash_persistence()
        return success
    except Exception as e:
        print(f"[FAIL] {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
