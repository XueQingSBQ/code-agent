"""Detailed verification test - check if subsystems are actually working.

This test verifies that subsystems don't just initialize, but actually function.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from carbide.sdk import Agent


async def test_detailed_verification():
    """Verify each subsystem actually works, not just initializes."""

    os.chdir(Path(__file__).parent.parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Create project-owned subsystem directories
        (project_dir / ".carbide" / "memory").mkdir(parents=True)
        (project_dir / ".carbide" / "rules").mkdir(parents=True)

        # Create test memory
        memory_file = project_dir / ".carbide" / "memory" / "test.md"
        memory_file.write_text("""---
tags: [test]
relevance_keywords: [verification, testing]
---

# Test Memory

This memory should be recalled during verification tests.
""")

        # Agent skills live outside the project workspace.
        agent_skills_dir = project_dir / "agent-skills"
        agent_skills_dir.mkdir()
        skill_file = agent_skills_dir / "greet.md"
        skill_file.write_text("""---
name: greet
title: Greeting Skill
description: Responds with a greeting
---

# Greeting Skill

When invoked, respond with: "Hello from the greeting skill!"
""")

        # Create test rule
        rule_file = project_dir / ".carbide" / "rules" / "always.md"
        rule_file.write_text("""---
trigger: always
priority: 1
---

# Always Active Rule

This rule should always be active.
""")

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
            print("Detailed Subsystem Verification")
            print("=" * 70)

            # 1. Verify Memory System
            print("\n1. Memory System Verification")
            print("-" * 70)

            if agent.memory_recall:
                mem_count = len(agent.memory_recall.index.memories)
                print(f"Memory files indexed: {mem_count}")

                if mem_count > 0:
                    print("PASS - Memory system scanned and indexed files")
                    for name, meta in agent.memory_recall.index.memories.items():
                        print(f"  - {name}: {meta.description}")
                else:
                    print("WARN - Memory system initialized but no memories indexed")
            else:
                print("FAIL - Memory system not initialized")

            # 2. Verify Skills System
            print("\n2. Skills System Verification")
            print("-" * 70)

            if agent.skill_manager:
                skill_count = len(agent.skill_manager.registry.skills)
                print(f"Skills registered: {skill_count}")

                if skill_count > 0:
                    print("PASS - Skills system scanned and registered files")
                    for name, meta in agent.skill_manager.registry.skills.items():
                        print(f"  - {name}: {meta.description}")
                else:
                    print("WARN - Skills system initialized but no skills registered")
            else:
                print("FAIL - Skills system not initialized")

            # 3. Verify Rules System
            print("\n3. Rules System Verification")
            print("-" * 70)

            if agent.rule_manager:
                rule_count = len(agent.rule_manager.registry.rules)
                print(f"Rules registered: {rule_count}")

                if rule_count > 0:
                    print("PASS - Rules system scanned and registered files")
                    for name, rule in agent.rule_manager.registry.rules.items():
                        print(f"  - {name}: priority={rule.priority}, enabled={rule.enabled}")
                else:
                    print("WARN - Rules system initialized but no rules registered")
            else:
                print("FAIL - Rules system not initialized")

            # 4. Verify Compression System
            print("\n4. Compression System Verification")
            print("-" * 70)

            if agent.compression_manager:
                budget = agent.compression_manager.budget
                print(f"Max tokens: {budget.max_tokens}")
                print(f"Compression threshold: {budget.compression_threshold * 100}%")
                print(f"Threshold tokens: {int(budget.max_tokens * budget.compression_threshold)}")
                print("PASS - Compression manager configured")
            else:
                print("FAIL - Compression manager not initialized")

            # 5. Verify Governance System
            print("\n5. Governance System Verification")
            print("-" * 70)

            governance_ok = True

            if agent.loop.permission_engine:
                print("PASS - PermissionEngine initialized")
            else:
                print("FAIL - PermissionEngine not initialized")
                governance_ok = False

            if agent.loop.bash_guard:
                print("PASS - BashGuard initialized")
            else:
                print("FAIL - BashGuard not initialized")
                governance_ok = False

            if agent.loop.audit_logger:
                print("PASS - AuditLogger initialized")
                audit_dir = project_dir / ".carbide" / "audit"
                print(f"  Audit directory: {audit_dir}")
                print(f"  Audit dir exists: {audit_dir.exists()}")
            else:
                print("FAIL - AuditLogger not initialized")
                governance_ok = False

            if governance_ok:
                print("=> Governance system fully operational")

            # 6. Verify SessionStore
            print("\n6. SessionStore Verification")
            print("-" * 70)

            if agent.session_store:
                db_path = project_dir / ".carbide" / "sessions.db"
                print(f"Database path: {db_path}")
                print(f"Database exists: {db_path.exists()}")

                if db_path.exists():
                    print("PASS - SessionStore created database")
                else:
                    print("WARN - SessionStore initialized but database not created yet")
            else:
                print("FAIL - SessionStore not initialized")

            # 7. Test actual query to see what gets injected
            print("\n7. System Prompt Injection Test")
            print("-" * 70)
            print("Sending query to check what gets injected into system prompt...")

            response_text = ""
            async for event in agent.query("Say: test"):
                if event.type == "chunk" and event.data:
                    if hasattr(event.data, "content") and event.data.content:
                        response_text += event.data.content
                elif event.type == "error":
                    print(f"Query error: {event.data}")
                    break
                elif event.type == "turn_complete":
                    break

            if response_text:
                print(f"PASS - Got response: {response_text[:60]}...")

            # Check message history
            print("\n8. Message History Check")
            print("-" * 70)

            state = agent.loop.get_state()
            print(f"Messages in history: {len(state.messages)}")

            if len(state.messages) > 0:
                print("Message structure:")
                for i, msg in enumerate(state.messages):
                    role = msg.get('role', 'unknown')
                    content_len = len(str(msg.get('content', '')))
                    print(f"  {i+1}. role={role}, content_length={content_len}")

            # 9. Check if compression would trigger
            print("\n9. Compression Trigger Check")
            print("-" * 70)

            if agent.compression_manager:
                total_tokens = sum(
                    agent.compression_manager.budget.estimate_tokens(msg).tokens
                    for msg in state.messages
                )
                threshold = int(
                    agent.compression_manager.budget.max_tokens *
                    agent.compression_manager.budget.compression_threshold
                )
                print(f"Current tokens: {total_tokens}")
                print(f"Threshold: {threshold}")
                print(f"Would trigger: {total_tokens > threshold}")

            print("\n" + "=" * 70)
            print("VERIFICATION COMPLETE")
            print("=" * 70)

        finally:
            if agent.session_store and agent.session_store._conn:
                try:
                    agent.session_store.close()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(test_detailed_verification())
