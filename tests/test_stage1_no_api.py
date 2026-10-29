"""
阶段1集成测试 - 不需要API密钥

测试统一Executor和治理层的集成
"""

from pathlib import Path
import tempfile
import sys


def run_stage1_integration():
    """测试阶段1的集成"""

    print("=" * 60)
    print("阶段1集成测试：统一Executor + 治理层")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # 测试1: 导入governance模块
        print("\n[测试1] 导入governance模块...")
        try:
            from carbide.governance import PermissionEngine, HookRegistry, AuditLogger
            from carbide.governance.bashguard import BashGuard

            print("  OK - 所有治理类可以导入")
        except ImportError as e:
            print(f"  FAIL - 导入失败: {e}")
            return False

        # 测试2: 创建治理组件
        print("\n[测试2] 创建治理组件...")
        try:
            perm = PermissionEngine(default_policy="allow", workspace=project_dir)
            audit = AuditLogger(project_dir / "audit")
            guard = BashGuard()
            print("  OK - PermissionEngine")
            print("  OK - AuditLogger")
            print("  OK - BashGuard")
        except Exception as e:
            print(f"  FAIL - 创建失败: {e}")
            return False

        # 测试3: 统一Executor导入
        print("\n[测试3] 统一Executor...")
        try:
            from carbide.engine.executor import StreamingToolExecutor
            from carbide.tools.registry import ToolRegistry
            from carbide.tools.base import ToolContext

            registry = ToolRegistry()
            ctx = ToolContext(workspace=project_dir)

            # 创建带治理的executor
            executor = StreamingToolExecutor(
                registry=registry,
                ctx=ctx,
                permission_engine=perm,
                audit_logger=audit,
                bash_guard=guard,
            )
            print("  OK - StreamingToolExecutor支持治理参数")
        except Exception as e:
            print(f"  FAIL - Executor创建失败: {e}")
            return False

        # 测试4: QueryLoop接受治理参数
        print("\n[测试4] QueryLoop配置...")
        try:
            from carbide.engine.loop import QueryLoop
            from carbide.providers.anthropic import AnthropicProvider

            # 创建mock provider (不需要API密钥)
            class MockProvider:
                model = "mock"

                async def stream(self, messages, system, tools):
                    return
                    yield  # Make it a generator

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

            # 验证参数传递
            assert loop.permission_engine is perm
            assert loop.audit_logger is audit
            assert loop.bash_guard is guard

            print("  OK - QueryLoop接受治理参数")
            print("  OK - 参数正确传递")
        except Exception as e:
            print(f"  FAIL - QueryLoop配置失败: {e}")
            return False

        # 测试5: SDK集成（跳过API密钥检查）
        print("\n[测试5] SDK集成架构...")
        try:
            # 只检查SDK代码结构，不实际初始化
            from carbide.sdk import Agent
            import inspect

            # 检查Agent.__init__是否有enable_governance参数
            sig = inspect.signature(Agent.__init__)
            params = list(sig.parameters.keys())

            if "enable_governance" in params:
                print("  OK - Agent支持enable_governance参数")
            else:
                print("  FAIL - Agent缺少enable_governance参数")
                return False

        except Exception as e:
            print(f"  FAIL - SDK检查失败: {e}")
            return False

        print("\n" + "=" * 60)
        print("阶段1集成测试：PASS")
        print("=" * 60)

        print("\n摘要:")
        print("  • 治理组件可以正确导入和创建")
        print("  • StreamingToolExecutor支持治理参数")
        print("  • QueryLoop接受并传递治理组件")
        print("  • SDK架构支持enable_governance")
        print("\n阶段1完成 - 统一Executor和治理层已集成")

        return True


if __name__ == "__main__":
    success = run_stage1_integration()
    sys.exit(0 if success else 1)
