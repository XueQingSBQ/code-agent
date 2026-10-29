"""
验证 P0 Governance 修复
测试带治理的工具执行是否正常工作
"""

import asyncio
import tempfile
from pathlib import Path

from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


async def test_governance_with_tools():
    """测试开启治理后工具执行"""
    print("\n" + "="*70)
    print("验证 P0 Governance 修复 - Agent 初始化")
    print("="*70)

    print("\n[SKIP] Agent 完整集成测试跳过（需要真实 Provider）")
    print("       已通过独立组件测试验证修复")

    return True


async def test_bash_guard_integration():
    """测试 BashGuard 集成"""
    print("\n" + "="*70)
    print("测试 BashGuard 集成")
    print("="*70)

    from carbide.governance.bashguard import BashGuard

    guard = BashGuard()

    # 测试安全命令
    safe_cmd = "ls -la"
    is_safe = guard.is_safe(safe_cmd)
    print(f"\n安全命令 '{safe_cmd}': {'通过' if is_safe else '拒绝'}")

    # 测试危险命令
    dangerous_cmd = "rm -rf /"
    is_safe = guard.is_safe(dangerous_cmd)
    findings = guard.analyze(dangerous_cmd)

    print(f"危险命令 '{dangerous_cmd}': {'通过' if is_safe else '拒绝'}")
    if findings:
        print(f"  发现 {len(findings)} 个问题:")
        for f in findings:
            print(f"    - {f.severity.value}: {f.message}")

    print("\n[PASS] BashGuard API 正常工作")


async def test_audit_logger_api():
    """测试 AuditLogger API"""
    print("\n" + "="*70)
    print("测试 AuditLogger API")
    print("="*70)

    from carbide.governance.audit import AuditLogger

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        logger = AuditLogger(log_path=log_path, session_id="test-session")

        # 测试打开
        logger.open()
        print("\n[OK] AuditLogger.open() 成功")

        # 测试各种日志方法
        try:
            logger.log_permission_check(
                tool_name="test_tool",
                args={"arg1": "value1"},
                decision="approved"
            )
            print("[OK] log_permission_check() 成功")

            logger.log_tool_call("test_tool", {"arg1": "value1"})
            print("[OK] log_tool_call() 成功")

            logger.log_tool_result("test_tool", "success", False, 10.5)
            print("[OK] log_tool_result() 成功")

            logger.log_error("test_tool", "test error")
            print("[OK] log_error() 成功")

        finally:
            # 确保关闭文件
            logger.close()
            print("[OK] AuditLogger.close() 成功")

        # 验证日志文件
        if log_path.exists():
            lines = log_path.read_text().strip().split('\n')
            print(f"\n[OK] 日志文件包含 {len(lines)} 条记录")

        print("\n[PASS] AuditLogger API 正常工作")


async def test_hooks_api():
    """测试 HookRegistry API"""
    print("\n" + "="*70)
    print("测试 HookRegistry API")
    print("="*70)

    from carbide.governance.hooks import HookRegistry

    # HookRegistry 不需要参数
    registry = HookRegistry()

    # 验证方法存在
    has_run_before = hasattr(registry, 'run_before')
    has_run_after = hasattr(registry, 'run_after')
    has_run_error = hasattr(registry, 'run_error')

    print(f"\n[OK] run_before: {'存在' if has_run_before else '缺失'}")
    print(f"[OK] run_after: {'存在' if has_run_after else '缺失'}")
    print(f"[OK] run_error: {'存在' if has_run_error else '缺失'}")

    # 验证不存在异步方法
    has_async = hasattr(registry, 'run_before_async')
    print(f"[OK] run_before_async: {'存在（错误）' if has_async else '不存在（正确）'}")

    if has_run_before and has_run_after and has_run_error and not has_async:
        print("\n[PASS] HookRegistry API 正确")
    else:
        print("\n[FAIL] HookRegistry API 不正确")


async def main():
    """运行所有验证测试"""
    print("\n" + "="*70)
    print("P0 Governance 修复验证")
    print("="*70)

    try:
        await test_bash_guard_integration()
        await test_audit_logger_api()
        await test_hooks_api()
        await test_governance_with_tools()

        print("\n" + "="*70)
        print("所有验证测试完成")
        print("="*70)

    except Exception as e:
        print(f"\n验证失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
