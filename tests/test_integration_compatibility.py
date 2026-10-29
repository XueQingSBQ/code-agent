"""简化的集成验证 - 不依赖真实API，只验证组件兼容性"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def run_imports():
    """测试所有新增模块可以正常导入"""
    print("\n[TEST 1/5] 模块导入测试...")

    try:
        from carbide.tools.bash_session import BashSession

        print("  [PASS] BashSession 导入成功")

        from carbide.storage.sqlite import SessionStorage

        print("  [PASS] SessionStorage 导入成功")

        from carbide.tools.shell import BashTool

        print("  [PASS] BashTool 导入成功")

        from carbide.tools.defaults import create_default_registry

        print("  [PASS] create_default_registry 导入成功")

        from carbide.sdk import Agent

        print("  [PASS] Agent 导入成功")

        return True
    except Exception as e:
        print(f"  [FAIL] 导入失败: {e}")
        return False


def run_tool_registry():
    """测试BashTool是否正确注册到默认registry"""
    print("\n[TEST 2/5] ToolRegistry集成测试...")

    try:
        from carbide.tools.defaults import create_default_registry

        registry = create_default_registry()
        print(f"  [PASS] Registry创建成功")

        # 检查tools属性（应该是_tools）
        tools = registry._tools
        print(f"  [PASS] 注册工具数: {len(tools)}")

        # 验证bash工具已注册
        if "bash" in tools:
            print("  [PASS] BashTool已注册")
        else:
            print("  [FAIL] BashTool未注册")
            print(f"  可用工具: {list(tools.keys())}")
            return False

        # 验证其他基础工具
        expected = ["read", "write", "edit", "grep", "glob", "bash"]
        missing = [t for t in expected if t not in tools]
        if missing:
            print(f"  [WARN] 缺少工具: {missing}")
        else:
            print(f"  [PASS] 所有基础工具已注册")

        return True
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback

        traceback.print_exc()
        return False


def run_bash_session():
    """测试BashSession可以独立工作"""
    print("\n[TEST 3/5] BashSession功能测试...")

    try:
        import asyncio
        from carbide.tools.bash_session import BashSession

        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                session = BashSession(workspace=tmpdir, shell_backend="bash")

                # 测试基础执行
                output, code = await session.execute("echo 'hello'")
                assert code == 0, f"Expected exit code 0, got {code}"
                assert "hello" in output, f"Expected 'hello' in output: {output}"
                print("  [PASS] 基础命令执行")

                # 测试状态持久化
                await session.execute("export TEST_VAR='test123'")
                output2, code2 = await session.execute("echo $TEST_VAR")
                assert code2 == 0
                assert "test123" in output2, f"Env var not persisted: {output2}"
                print("  [PASS] 环境变量持久化")

                await session.close()
                print("  [PASS] Session关闭")

                return True

        result = asyncio.run(run_test())
        return result

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback

        traceback.print_exc()
        return False


def run_sqlite_storage():
    """测试SQLite存储功能"""
    print("\n[TEST 4/5] SQLite存储测试...")

    try:
        from carbide.storage.sqlite import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # 创建并使用storage
            storage = SessionStorage(db_path)
            print("  [PASS] Storage创建")

            session_id = "test-001"
            storage.create_session(session_id, tmpdir, "test-model")
            print("  [PASS] Session创建")

            storage.add_message(session_id, "user", "test message")
            print("  [PASS] 消息添加")

            messages = storage.get_messages(session_id)
            assert len(messages) == 1
            print("  [PASS] 消息读取")

            # 关闭连接
            storage.close()
            print("  [PASS] Storage关闭")

            # 验证持久化
            storage2 = SessionStorage(db_path)
            session = storage2.get_session(session_id)
            assert session is not None
            print("  [PASS] 数据持久化")

            storage2.close()

            return True

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback

        traceback.print_exc()
        return False


def run_bash_tool_integration():
    """测试BashTool与BashSession的集成"""
    print("\n[TEST 5/5] BashTool集成测试...")

    try:
        import asyncio
        from carbide.tools.shell import BashTool, BashParams
        from carbide.tools.base import ToolContext

        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = BashTool()
                ctx = ToolContext(workspace=tmpdir, shell="bash")

                try:
                    # 测试执行
                    result1 = await tool.run(BashParams(command="export MY_VAR='test'"), ctx)
                    assert not result1.is_error, f"Command failed: {result1.content}"
                    print("  [PASS] BashTool命令执行")

                    # 测试状态持久化
                    result2 = await tool.run(BashParams(command="echo $MY_VAR"), ctx)
                    assert not result2.is_error
                    assert "test" in result2.content, f"State not persisted: {result2.content}"
                    print("  [PASS] BashTool状态持久化")
                    return True
                finally:
                    await BashTool.close_sessions()

        result = asyncio.run(run_test())
        return result

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    print("=" * 60)
    print("Carbide 组件兼容性验证")
    print("=" * 60)
    print("\n本测试验证M1/M2组件是否正确集成，不依赖真实API")

    tests = [
        ("模块导入", run_imports),
        ("ToolRegistry", run_tool_registry),
        ("BashSession", run_bash_session),
        ("SQLite存储", run_sqlite_storage),
        ("BashTool集成", run_bash_tool_integration),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            result = test_func()
            if result:
                passed += 1
                print(f"[OK] {name} 通过\n")
            else:
                failed += 1
                print(f"[ERROR] {name} 失败\n")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {name} 异常: {e}\n")
            import traceback

            traceback.print_exc()

    print("=" * 60)
    print(f"结果: {passed}/{len(tests)} 通过")
    print("=" * 60)

    if failed == 0:
        print("\n[OK] 所有组件兼容性测试通过")
        print("\n验证项:")
        print("  [PASS] 新增模块可以导入")
        print("  [PASS] BashTool已注册到默认registry")
        print("  [PASS] BashSession独立运行正常")
        print("  [PASS] SQLite存储功能正常")
        print("  [PASS] BashTool与BashSession集成正常")
        print("\n结论: M1和M2核心组件已正确集成到系统中")
        return True
    else:
        print(f"\n[ERROR] 有 {failed} 个测试失败")
        print("需要修复集成问题")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
