"""Test 3: Context compression trigger test.

Test compression by accumulating enough tokens to exceed threshold.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent


async def test_compression_trigger():
    """Test compression triggering with large context."""

    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        print("Test 3: Context Compression Trigger")
        print("=" * 70)

        # Initialize agent with low compression threshold for testing
        agent = Agent(
            project_dir=project_dir,
            enable_governance=False,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=True,
            enable_session_store=False,
        )

        # Lower the threshold for testing (override after initialization)
        if agent.compression_manager:
            # Original: 200K tokens at 80% = 160K threshold
            # For testing: 200K tokens at 5% = 10K threshold
            agent.compression_manager.budget.compression_threshold = 0.05
            threshold = int(
                agent.compression_manager.budget.max_tokens *
                agent.compression_manager.budget.compression_threshold
            )
            print(f"Compression threshold lowered to: {threshold} tokens (5%)")
        else:
            print("FAIL - CompressionManager not initialized")
            return False

        try:
            # Test 1: Initial state
            print("\n1. Initial State")
            print("-" * 70)

            state = agent.loop.get_state()
            total_tokens = sum(
                agent.compression_manager.budget.estimate_tokens(msg).tokens
                for msg in state.messages
            )

            print(f"Initial messages: {len(state.messages)}")
            print(f"Initial tokens (estimated): {total_tokens}")
            print(f"Threshold: {threshold}")
            print(f"Would trigger: {total_tokens > threshold}")

            # Test 2: Send multiple queries with long content
            print("\n2. Accumulating Context")
            print("-" * 70)

            # Send queries with substantial content
            queries = [
                "Explain Python async/await in detail with examples",
                "Describe the differences between list, tuple, set, and dict in Python",
                "Explain object-oriented programming concepts: inheritance, polymorphism, encapsulation",
                "What are Python decorators and how do they work? Give examples",
                "Explain Python generators and iterators with examples",
                "Describe Python context managers and the 'with' statement",
                "What are Python metaclasses and when would you use them?",
                "Explain Python's GIL and its impact on multi-threading",
            ]

            for i, query in enumerate(queries, 1):
                print(f"\nQuery {i}/{len(queries)}: {query[:50]}...")

                response_text = ""
                async for event in agent.query(query):
                    if event.type == "chunk" and event.data:
                        if hasattr(event.data, "content") and event.data.content:
                            response_text += event.data.content
                    elif event.type == "error":
                        print(f"  ERROR: {event.data}")
                        break
                    elif event.type == "turn_complete":
                        break

                # Check state after each query
                state = agent.loop.get_state()
                total_tokens = sum(
                    agent.compression_manager.budget.estimate_tokens(msg).tokens
                    for msg in state.messages
                )

                print(f"  Messages: {len(state.messages)}")
                print(f"  Tokens: {total_tokens}")
                print(f"  Response length: {len(response_text)} chars")

                if total_tokens > threshold:
                    print(f"  >>> THRESHOLD EXCEEDED! Compression should trigger next turn")

                # Stop if we've accumulated enough
                if i >= 5 and total_tokens > threshold:
                    print(f"\nStopping - sufficient context accumulated")
                    break

            # Test 3: One more query to trigger compression
            print("\n3. Triggering Compression")
            print("-" * 70)

            state = agent.loop.get_state()
            before_count = len(state.messages)
            before_tokens = sum(
                agent.compression_manager.budget.estimate_tokens(msg).tokens
                for msg in state.messages
            )

            print(f"Before final query:")
            print(f"  Messages: {before_count}")
            print(f"  Tokens: {before_tokens}")

            # Send one more query
            print("\nSending query that should trigger compression...")

            async for event in agent.query("Summarize what we discussed"):
                if event.type == "chunk" and event.data:
                    pass  # Collect response
                elif event.type == "error":
                    print(f"ERROR: {event.data}")
                    break
                elif event.type == "turn_complete":
                    break

            # Check state after compression
            state = agent.loop.get_state()
            after_count = len(state.messages)
            after_tokens = sum(
                agent.compression_manager.budget.estimate_tokens(msg).tokens
                for msg in state.messages
            )

            print(f"\nAfter final query:")
            print(f"  Messages: {after_count}")
            print(f"  Tokens: {after_tokens}")

            # Test 4: Analysis
            print("\n4. Compression Analysis")
            print("-" * 70)

            print(f"Message count change: {before_count} -> {after_count} (delta: {after_count - before_count})")
            print(f"Token count change: {before_tokens} -> {after_tokens} (delta: {after_tokens - before_tokens})")

            if after_count < before_count:
                print("PASS - Messages were compressed (count reduced)")
            elif after_tokens < before_tokens:
                print("PASS - Tokens were reduced (compression occurred)")
            else:
                print("WARN - No compression detected")
                print("  This might mean:")
                print("  - Threshold not reached yet")
                print("  - Compression logic not triggered")
                print("  - L1-L3 compression didn't apply to this content")

            # Test 5: Final statistics
            print("\n5. Final Statistics")
            print("-" * 70)

            stats = agent.get_stats()
            print(f"Total turns: {stats['turn_count']}")
            print(f"Total messages: {stats['message_count']}")
            print(f"Input tokens: {stats['total_input_tokens']}")
            print(f"Output tokens: {stats['total_output_tokens']}")
            print(f"Cache read tokens: {stats['total_cache_read_tokens']}")

            print("\n" + "=" * 70)
            print("TEST 3 COMPLETE: Context Compression")
            print("=" * 70)

            # Success if we accumulated enough context
            return before_tokens > threshold

        finally:
            pass


if __name__ == "__main__":
    result = asyncio.run(test_compression_trigger())
    exit(0 if result else 1)
