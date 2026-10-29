"""
阶段1集成测试 - 修正API版本

测试统一Executor和治理层的集成，使用正确的API
"""

from pathlib import Path
import tempfile
import sys


def run_stage1_integration():
    """测试阶段1的集成"""

    print("=" * 60)
    print("Stage 1 Integration Test: Unified Executor + Governance")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # Test 1: Import governance modules
        print("\n[Test 1] Import governance modules...")
        try:
            from carbide.governance import (
                PermissionEngine,
                HookRegistry,
                AuditLogger,
                PermissionMode,
            )
            from carbide.governance.bashguard import BashGuard

            print("  PASS - All governance classes imported")
        except ImportError as e:
            print(f"  FAIL - Import failed: {e}")
            return False

        # Test 2: Create governance components with correct API
        print("\n[Test 2] Create governance components...")
        try:
            # PermissionEngine只需要mode参数
            perm = PermissionEngine(mode=PermissionMode.PROMPT)
            print("  PASS - PermissionEngine created")

            # AuditLogger需要audit_dir和session_id
            import uuid

            audit = AuditLogger(project_dir / "audit", str(uuid.uuid4()))
            print("  PASS - AuditLogger created")

            # BashGuard不需要参数
            guard = BashGuard()
            print("  PASS - BashGuard created")

        except Exception as e:
            print(f"  FAIL - Creation failed: {e}")
            import traceback

            traceback.print_exc()
            return False

        # Test 3: Unified Executor import
        print("\n[Test 3] Unified Executor...")
        try:
            from carbide.engine.executor import StreamingToolExecutor
            from carbide.tools.registry import ToolRegistry
            from carbide.tools.base import ToolContext

            registry = ToolRegistry()
            ctx = ToolContext(workspace=project_dir)

            # Create executor with governance
            executor = StreamingToolExecutor(
                registry=registry,
                ctx=ctx,
                permission_engine=perm,
                audit_logger=audit,
                bash_guard=guard,
            )
            print("  PASS - StreamingToolExecutor supports governance parameters")

            # Verify parameters
            assert executor._permission is perm
            assert executor._audit is audit
            assert executor._bash_guard is guard
            print("  PASS - Parameters correctly stored")

        except Exception as e:
            print(f"  FAIL - Executor creation failed: {e}")
            import traceback

            traceback.print_exc()
            return False

        # Test 4: QueryLoop accepts governance parameters
        print("\n[Test 4] QueryLoop configuration...")
        try:
            from carbide.engine.loop import QueryLoop

            # Create mock provider
            class MockProvider:
                model = "mock"

                async def stream(self, messages, system, tools):
                    return
                    yield

            provider = MockProvider()

            loop = QueryLoop(
                provider=provider,
                tool_registry=registry,
                workspace=project_dir,
                permission_engine=perm,
                hook_registry=None,
                audit_logger=audit,
                bash_guard=guard,
            )

            # Verify parameters passed
            assert loop.permission_engine is perm
            assert loop.audit_logger is audit
            assert loop.bash_guard is guard

            print("  PASS - QueryLoop accepts governance parameters")
            print("  PASS - Parameters correctly passed")

        except Exception as e:
            print(f"  FAIL - QueryLoop configuration failed: {e}")
            import traceback

            traceback.print_exc()
            return False

        # Test 5: SDK architecture
        print("\n[Test 5] SDK architecture...")
        try:
            from carbide.sdk import Agent
            import inspect

            # Check Agent.__init__ has enable_governance parameter
            sig = inspect.signature(Agent.__init__)
            params = list(sig.parameters.keys())

            if "enable_governance" in params:
                print("  PASS - Agent supports enable_governance parameter")
            else:
                print("  FAIL - Agent missing enable_governance parameter")
                return False

        except Exception as e:
            print(f"  FAIL - SDK check failed: {e}")
            return False

        print("\n" + "=" * 60)
        print("Stage 1 Integration Test: ALL PASS")
        print("=" * 60)

        print("\nSummary:")
        print("  * Governance components correctly imported and created")
        print("  * StreamingToolExecutor supports governance parameters")
        print("  * QueryLoop accepts and passes governance components")
        print("  * SDK architecture supports enable_governance")
        print("\nStage 1 Complete - Unified Executor and Governance Integrated")

        return True


if __name__ == "__main__":
    success = run_stage1_integration()
    sys.exit(0 if success else 1)
