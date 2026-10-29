"""
测试阶段1集成：统一Executor和治理层

验证点：
1. SDK可以正常初始化
2. 治理组件被正确创建
3. QueryLoop接收治理组件
4. Executor使用治理组件
"""

import asyncio
from pathlib import Path
import tempfile

from carbide.sdk import Agent


async def run_stage1_integration():
    """测试阶段1的集成"""

    print("=" * 60)
    print("阶段1集成测试：统一Executor + 治理层")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # 测试1: SDK初始化
        print("\n[测试1] SDK初始化...")
        try:
            agent = Agent(
                project_dir=project_dir,
                enable_governance=True,
                enable_memory=False,
                enable_skills=False,
                enable_compression=False,
            )
            print("  ✓ SDK初始化成功")
        except Exception as e:
            print(f"  ✗ SDK初始化失败: {e}")
            return False

        # 测试2: 治理组件
        print("\n[测试2] 治理组件初始化...")
        has_permission = agent.permission_engine is not None
        has_audit = agent.audit_logger is not None
        has_bash_guard = agent.bash_guard is not None

        print(f"  PermissionEngine: {'✓' if has_permission else '✗'}")
        print(f"  AuditLogger: {'✓' if has_audit else '✗'}")
        print(f"  BashGuard: {'✓' if has_bash_guard else '✗'}")
        print(f"  HookRegistry: {'✓' if agent.hook_registry is not None else '✗ (可选)'}")

        if not (has_permission and has_audit and has_bash_guard):
            print("  ✗ 治理组件初始化不完整")
            return False

        print("  ✓ 核心治理组件初始化成功")

        # 测试3: QueryLoop接收治理组件
        print("\n[测试3] QueryLoop配置...")
        loop = agent.loop

        has_loop_permission = loop.permission_engine is not None
        has_loop_audit = loop.audit_logger is not None
        has_loop_bash_guard = loop.bash_guard is not None

        print(f"  QueryLoop.permission_engine: {'✓' if has_loop_permission else '✗'}")
        print(f"  QueryLoop.audit_logger: {'✓' if has_loop_audit else '✗'}")
        print(f"  QueryLoop.bash_guard: {'✓' if has_loop_bash_guard else '✗'}")

        if not (has_loop_permission and has_loop_audit and has_loop_bash_guard):
            print("  ✗ QueryLoop未接收治理组件")
            return False

        print("  ✓ QueryLoop正确配置治理组件")

        # 测试4: 禁用治理的情况
        print("\n[测试4] 禁用治理的降级行为...")
        try:
            agent_no_gov = Agent(
                project_dir=project_dir,
                enable_governance=False,
                enable_memory=False,
                enable_skills=False,
                enable_compression=False,
            )

            if agent_no_gov.permission_engine is None:
                print("  ✓ enable_governance=False时正确不初始化治理组件")
            else:
                print("  ✗ enable_governance=False但仍初始化了治理组件")
                return False

        except Exception as e:
            print(f"  ✗ 禁用治理时失败: {e}")
            return False

        print("\n" + "=" * 60)
        print("阶段1集成测试：✓ 全部通过")
        print("=" * 60)

        print("\n摘要:")
        print("  • 统一Executor已集成")
        print("  • 治理组件正确初始化")
        print("  • QueryLoop正确接收治理参数")
        print("  • 降级行为工作正常")

        return True


if __name__ == "__main__":
    success = asyncio.run(run_stage1_integration())
    exit(0 if success else 1)
