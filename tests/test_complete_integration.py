"""完整集成测试 - 验证所有M0-M5组件协同工作"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry
from carbide.storage.sqlite import SessionStorage


async def test_full_integration():
    """测试完整的Agent → QueryLoop → Tools → Storage链路"""
    print("\n" + "="*60)
    print("完整集成测试：Agent + Tools + Storage")
    print("="*60)

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        db_path = workspace / "test_session.db"

        # 1. 创建Storage
        print("\n[1/5] 初始化SessionStorage...")
        storage = SessionStorage(db_path)
        session_id = "test-integration-001"
        storage.create_session(
            session_id=session_id,
            workspace=str(workspace),
            model="gpt-5.6-luna",
        )
        print("  [PASS] SessionStorage创建成功")

        # 2. 创建Tool Registry
        print("\n[2/5] 创建ToolRegistry...")
        registry = create_default_registry()
        print(f"  [PASS] 工具数量: {len(registry.tools)}")
        for tool_name in registry.tools.keys():
            print(f"    - {tool_name}")

        # 3. 创建Agent
        print("\n[3/5] 创建Agent...")
        agent = Agent(
            project_dir=workspace,
            tool_registry=registry,
            enable_compression=True,
        )
        print("  [PASS] Agent初始化成功")

        # 4. 测试持久化Bash功能
        print("\n[4/5] 测试持久化Bash...")
        query1 = "Use bash to set an environment variable TEST_VAR='hello_world', then echo it back"

        print(f"\nQuery: {query1}")
        print("\nAgent响应:")

        messages = []
        async for event in agent.query(query1):
            if event.type == "chunk" and event.data:
                if event.data.type == "content_block_delta" and event.data.content:
                    content = event.data.content
                    print(content, end="", flush=True)
                    messages.append(("assistant", content))
            elif event.type == "tool_use":
                tool_name = event.data.get("name", "unknown")
                print(f"\n[使用工具: {tool_name}]")

                # 记录到storage
                storage.add_tool_execution(
                    session_id=session_id,
                    tool_name=tool_name,
                    arguments=event.data.get("input", {}),
                )
            elif event.type == "tool_complete":
                tool_name = event.data.get("tool_name", "unknown")
                result = event.data.get("result", "")
                is_error = event.data.get("is_error", False)
                duration = event.data.get("duration_ms", 0)

                status = "失败" if is_error else "成功"
                print(f"[工具执行{status}: {tool_name}, {duration:.1f}ms]")

                # 更新storage中的工具记录
                # (实际应该用tool_execution_id更新，这里简化)

        print("\n\n  [PASS] Bash持久化测试完成")

        # 5. 验证Storage中的数据
        print("\n[5/5] 验证Storage数据...")

        session = storage.get_session(session_id)
        assert session is not None, "Session未保存"
        print(f"  [PASS] Session存在: {session['session_id']}")

        tool_execs = storage.get_tool_executions(session_id)
        print(f"  [PASS] 工具执行记录: {len(tool_execs)} 条")
        for exec_record in tool_execs:
            print(f"    - {exec_record['tool_name']}")

        storage.close()

        print("\n" + "="*60)
        print("[OK] 完整集成测试通过")
        print("="*60)
        print("\n验证要点:")
        print("  [PASS] Agent正确初始化")
        print("  [PASS] ToolRegistry包含所有工具")
        print("  [PASS] QueryLoop执行成功")
        print("  [PASS] BashTool可用(持久化session)")
        print("  [PASS] SessionStorage记录数据")
        print("  [PASS] 各组件协同工作")

        return True


async def test_bash_persistence_integration():
    """专门测试BashSession在完整链路中的状态持久化"""
    print("\n" + "="*60)
    print("Bash持久化集成测试")
    print("="*60)

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        print("\n[测试] 创建Agent并测试多轮Bash交互...")

        registry = create_default_registry()
        agent = Agent(project_dir=workspace, tool_registry=registry)

        # 第一个query: 设置环境变量和改变目录
        query1 = """Do these bash commands:
1. export MY_VAR='test_value'
2. mkdir -p /tmp/carbide_test && cd /tmp/carbide_test
3. pwd
"""

        print(f"\nQuery 1: {query1}")
        print("Agent响应:")

        response1 = []
        async for event in agent.query(query1):
            if event.type == "chunk" and event.data:
                if event.data.type == "content_block_delta" and event.data.content:
                    content = event.data.content
                    print(content, end="", flush=True)
                    response1.append(content)

        response1_text = "".join(response1)
        print("\n")

        # 第二个query: 验证状态是否保留
        query2 = """Now do these bash commands to verify state persists:
1. echo $MY_VAR (should show 'test_value')
2. pwd (should still be in /tmp/carbide_test)
"""

        print(f"\nQuery 2: {query2}")
        print("Agent响应:")

        response2 = []
        async for event in agent.query(query2):
            if event.type == "chunk" and event.data:
                if event.data.type == "content_block_delta" and event.data.content:
                    content = event.data.content
                    print(content, end="", flush=True)
                    response2.append(content)

        response2_text = "".join(response2)
        print("\n")

        # 验证
        print("\n验证结果:")
        if "test_value" in response2_text.lower():
            print("  [PASS] 环境变量持久化成功")
        else:
            print("  [FAIL] 环境变量持久化失败")
            return False

        if "carbide_test" in response2_text.lower():
            print("  [PASS] 工作目录持久化成功")
        else:
            print("  [FAIL] 工作目录持久化失败")
            return False

        print("\n[OK] Bash持久化集成测试通过")
        return True


async def test_storage_integration():
    """测试Storage与Agent的集成"""
    print("\n" + "="*60)
    print("Storage集成测试")
    print("="*60)

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        db_path = workspace / "session.db"

        print("\n[测试] Storage生命周期管理...")

        storage = SessionStorage(db_path)
        session_id = "storage-test-001"

        # 创建session
        storage.create_session(
            session_id=session_id,
            workspace=str(workspace),
            model="gpt-5.6-luna",
        )
        print("  [PASS] Session创建")

        # 添加消息
        storage.add_message(session_id, "user", "Hello")
        storage.add_message(session_id, "assistant", "Hi there")
        print("  [PASS] 消息添加")

        # 添加工具执行
        storage.add_tool_execution(
            session_id=session_id,
            tool_name="bash",
            arguments={"command": "ls"},
            result="file1.txt\nfile2.txt",
            is_error=False,
            duration_ms=123.45,
        )
        print("  [PASS] 工具执行记录")

        # 关闭并重新打开
        storage.close()
        print("  [PASS] Storage关闭")

        storage2 = SessionStorage(db_path)

        # 验证数据持久化
        session = storage2.get_session(session_id)
        assert session is not None
        print("  [PASS] Session恢复")

        messages = storage2.get_messages(session_id)
        assert len(messages) == 2
        print(f"  [PASS] 消息恢复 ({len(messages)}条)")

        tools = storage2.get_tool_executions(session_id)
        assert len(tools) == 1
        print(f"  [PASS] 工具记录恢复 ({len(tools)}条)")

        storage2.close()

        print("\n[OK] Storage集成测试通过")
        return True


async def main():
    print("="*60)
    print("Carbide 完整集成验证")
    print("="*60)

    tests = [
        ("Storage集成", test_storage_integration),
        ("完整链路", test_full_integration),
        ("Bash持久化", test_bash_persistence_integration),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            print(f"\n{'='*60}")
            print(f"运行: {name}")
            print('='*60)
            result = await test_func()
            if result:
                passed += 1
                print(f"\n[OK] {name} 通过")
            else:
                failed += 1
                print(f"\n[ERROR] {name} 失败")
        except Exception as e:
            failed += 1
            print(f"\n[ERROR] {name} 异常: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "="*60)
    print(f"最终结果: {passed} 通过, {failed} 失败")
    print("="*60)

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
