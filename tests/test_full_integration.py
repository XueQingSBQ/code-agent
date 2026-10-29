"""Full integration test for all stages.

Tests all subsystems working together:
- Stage 1: Unified Executor + Governance
- Stage 2: Context Compression
- Stage 3: Memory/Skills/Rules
- Stage 4: SessionStore
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent


async def test_full_integration():
    """Test all subsystems integrated together."""

    # Change to project root so .env can be found
    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create project-owned state directories. Agent skills are separate.
        (project_dir / ".carbide" / "memory").mkdir(parents=True)
        (project_dir / ".carbide" / "rules").mkdir(parents=True)

        agent_skills_dir = project_dir / "agent-skills"
        agent_skills_dir.mkdir()
        skill_path = agent_skills_dir / "test.md"
        skill_path.write_text("""---
name: test
description: Test skill
---

# Test Skill

This is a test skill for integration testing.
""")

        # Initialize agent with all subsystems
        agent = Agent(
            project_dir=project_dir,
            enable_governance=True,
            enable_memory=True,
            enable_skills=True,
            agent_skills_dir=agent_skills_dir,
            enable_rules=True,
            enable_compression=True,
            enable_session_store=True,
        )

        try:
            print("Full Integration Test")
            print("=" * 60)

            # Test 1: All subsystems initialized
            print("\nTest 1: Subsystem Initialization")
            print("-" * 60)

            assert agent.compression_manager is not None, "CompressionManager not initialized"
            print("PASS - CompressionManager initialized")

            assert agent.memory_recall is not None, "MemoryRecall not initialized"
            print("PASS - MemoryRecall initialized")

            assert agent.skill_manager is not None, "SkillManager not initialized"
            print(f"PASS - SkillManager initialized ({len(agent.skill_manager.registry.skills)} skills)")

            assert agent.rule_manager is not None, "RuleManager not initialized"
            print("PASS - RuleManager initialized")

            assert agent.loop.permission_engine is not None, "PermissionEngine not initialized"
            print("PASS - PermissionEngine initialized")

            assert agent.loop.bash_guard is not None, "BashGuard not initialized"
            print("PASS - BashGuard initialized")

            assert agent.loop.audit_logger is not None, "AuditLogger not initialized"
            print("PASS - AuditLogger initialized")

            assert agent.session_store is not None, "SessionStore not initialized"
            print("PASS - SessionStore initialized")

            # Test 2: Simple query
            print("\nTest 2: Simple Query")
            print("-" * 60)

            response_text = ""
            async for event in agent.query("Say: Integration test"):
                if event.type == "chunk" and event.data:
                    if hasattr(event.data, "content") and event.data.content:
                        response_text += event.data.content
                elif event.type == "error":
                    raise AssertionError(f"Query failed: {event.data}")
                elif event.type == "turn_complete":
                    break

            assert response_text, "No response received"
            print(f"PASS - Got response: {response_text[:60]}...")

            # Test 3: Multiple turns (test compression)
            print("\nTest 3: Multiple Turns")
            print("-" * 60)

            for i in range(3):
                async for event in agent.query(f"Count to {i+1}"):
                    if event.type == "error":
                        raise AssertionError(f"Turn {i+1} failed: {event.data}")
                    elif event.type == "turn_complete":
                        break
                print(f"PASS - Turn {i+1} completed")

            # Test 4: Stats
            print("\nTest 4: Statistics")
            print("-" * 60)

            stats = agent.get_stats()
            print(f"PASS - Total turns: {stats['turn_count']}")
            print(f"PASS - Input tokens: {stats['total_input_tokens']}")
            print(f"PASS - Output tokens: {stats['total_output_tokens']}")
            print(f"PASS - Cache read tokens: {stats['total_cache_read_tokens']}")
            print(f"PASS - Message count: {stats['message_count']}")

            assert stats["turn_count"] == 4, f"Expected 4 turns, got {stats['turn_count']}"
            assert stats["total_input_tokens"] > 0, "No input tokens recorded"
            assert stats["total_output_tokens"] > 0, "No output tokens recorded"

            # Test 5: Database persistence
            print("\nTest 5: Database Persistence")
            print("-" * 60)

            db_path = project_dir / ".carbide" / "sessions.db"
            assert db_path.exists(), "Database file not created"
            print(f"PASS - Database file exists: {db_path}")

            # Close session store before cleanup
            if agent.session_store:
                agent.session_store.close()
                print("PASS - SessionStore closed")

            print("\n" + "=" * 60)
            print("ALL INTEGRATION TESTS PASSED")
            print("=" * 60)
            print("\nIntegration Summary:")
            print("  Stage 1: Unified Executor + Governance PASS")
            print("  Stage 2: Context Compression PASS")
            print("  Stage 3: Memory/Skills/Rules PASS")
            print("  Stage 4: SessionStore PASS")
            print("\nAll subsystems working together!")

        finally:
            # Ensure SessionStore is closed even on error
            if agent.session_store and agent.session_store._conn:
                try:
                    agent.session_store.close()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(test_full_integration())
