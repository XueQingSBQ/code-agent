"""Test API stability - check if 502 errors are intermittent."""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from carbide.providers.anthropic import AnthropicProvider

async def test_stability():
    """Test API with multiple consecutive calls."""

    provider = AnthropicProvider()

    print("Testing API stability (5 calls)...")
    print("=" * 60)

    success = 0
    errors = 0

    for i in range(5):
        print(f"\nCall {i+1}: ", end="", flush=True)
        try:
            chunks = 0
            async for chunk in provider.stream(
                messages=[{"role": "user", "content": "Say just 'hi'"}],
                system="You are a helpful assistant",
            ):
                chunks += 1

            print(f"[OK] Success ({chunks} chunks)")
            success += 1

        except Exception as e:
            error_str = str(e)
            if "502" in error_str or "bad gateway" in error_str.lower():
                print("[FAIL] 502 Bad Gateway")
            elif "503" in error_str:
                print("[FAIL] 503 Service Unavailable")
            elif "model_not_found" in error_str.lower():
                print("[FAIL] Model not found")
            else:
                print(f"[FAIL] {error_str[:80]}")
            errors += 1

        # Wait between calls
        await asyncio.sleep(1)

    print()
    print("=" * 60)
    print(f"Results: {success} success, {errors} errors")
    print(f"Success rate: {success/5*100:.0f}%")

if __name__ == "__main__":
    asyncio.run(test_stability())
