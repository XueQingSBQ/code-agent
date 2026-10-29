"""Test 1: Memory System with correct frontmatter format."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent


async def test_memory_system():
    """Test Memory system with correctly formatted files."""

    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        memory_dir = project_dir / ".carbide" / "memory"
        memory_dir.mkdir(parents=True)

        # Create correctly formatted memory files

        # 1. User memory
        (memory_dir / "user-profile.md").write_text("""---
name: user-profile
description: User profile and preferences
type: user
tags: profile, preferences
---

# User Profile

The user is a Python developer working on an AI agent framework.
Prefers concise explanations and practical examples.
""")

        # 2. Feedback memory
        (memory_dir / "code-style.md").write_text("""---
name: code-style
description: Coding style preferences
type: feedback
tags: style, coding
---

# Code Style Feedback

**Why:** User prefers descriptive variable names over abbreviations.

**How to apply:** Use full words like `message_count` instead of `msg_cnt`.
""")

        # 3. Project memory
        (memory_dir / "carbide-architecture.md").write_text("""---
name: carbide-architecture
description: Carbide six-layer architecture
type: project
tags: architecture, design
---

# Carbide Architecture

Six-layer design:
- M0: Provider abstraction
- M1: Tool execution
- M2: Session persistence
- M3: Context management
- M4: Memory and skills
- M5: CLI layer
""")

        # 4. Reference memory
        (memory_dir / "api-docs.md").write_text("""---
name: api-docs
description: API documentation links
type: reference
tags: docs, api
---

# API Documentation

- Anthropic API: https://docs.anthropic.com
- OpenAI API: https://platform.openai.com/docs
""")

        print("Test 1: Memory System Verification")
        print("=" * 70)

        # Initialize agent
        agent = Agent(
            project_dir=project_dir,
            enable_governance=False,
            enable_memory=True,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
            enable_session_store=False,
        )

        try:
            # Test 1: Check memory indexing
            print("\n1. Memory Indexing")
            print("-" * 70)

            if not agent.memory_recall:
                print("FAIL - MemoryRecall not initialized")
                return False

            mem_count = len(agent.memory_recall.index.memories)
            print(f"Memories indexed: {mem_count}")

            if mem_count == 0:
                print("FAIL - No memories indexed")
                return False

            print(f"PASS - {mem_count} memories indexed")

            # Show indexed memories
            print("\nIndexed memories:")
            for name, meta in agent.memory_recall.index.memories.items():
                print(f"  - {name}")
                print(f"    Type: {meta.type}")
                print(f"    Description: {meta.description}")
                print(f"    Tags: {meta.tags}")

            # Test 2: Check memory types
            print("\n2. Memory Type Distribution")
            print("-" * 70)

            types = {}
            for meta in agent.memory_recall.index.memories.values():
                types[meta.type.value] = types.get(meta.type.value, 0) + 1

            print("Memory types:")
            for mem_type, count in types.items():
                print(f"  - {mem_type}: {count}")

            expected_types = {"user", "feedback", "project", "reference"}
            if set(types.keys()) == expected_types:
                print("PASS - All memory types present")
            else:
                print(f"WARN - Missing types: {expected_types - set(types.keys())}")

            # Test 3: Memory recall (requires LLM)
            print("\n3. Memory Recall Test")
            print("-" * 70)

            print("Sending query that should trigger memory recall...")

            response_text = ""
            async for event in agent.query("What is the Carbide architecture?"):
                if event.type == "chunk" and event.data:
                    if hasattr(event.data, "content") and event.data.content:
                        response_text += event.data.content
                elif event.type == "error":
                    print(f"Query error: {event.data}")
                    break
                elif event.type == "turn_complete":
                    break

            if response_text:
                print(f"PASS - Got response: {response_text[:100]}...")

                # Check if response mentions memory content
                if "M0" in response_text or "six-layer" in response_text.lower():
                    print("PASS - Response appears to use memory content")
                else:
                    print("WARN - Response may not have used memory")
            else:
                print("FAIL - No response received")

            print("\n" + "=" * 70)
            print("TEST 1 COMPLETE: Memory System")
            print("=" * 70)
            return True

        finally:
            pass


if __name__ == "__main__":
    result = asyncio.run(test_memory_system())
    exit(0 if result else 1)
