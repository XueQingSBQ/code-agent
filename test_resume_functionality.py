"""
测试 --resume 功能的端到端集成
"""

import asyncio
from pathlib import Path


async def test_resume_workflow():
    """测试完整的创建-暂停-恢复工作流"""
    print("="*70)
    print("测试 Resume 功能集成")
    print("="*70)

    from carbide.governance.session_store import SessionStore
    from carbide.sdk import Agent
    from carbide.providers.anthropic import AnthropicProvider

    # 初始化 SessionStore
    store_path = Path.cwd() / ".carbide" / "sessions.db"
    store = SessionStore(store_path)
    store.open()

    try:
        # 阶段 1: 创建新会话并保存消息
        print("\n[阶段 1] 创建新会话...")
        test_session_id = "test-resume-session"

        # 清理旧测试会话
        if store.session_exists(test_session_id):
            store._conn.execute("DELETE FROM messages WHERE session_id = ?", (test_session_id,))
            store._conn.execute("DELETE FROM sessions WHERE id = ?", (test_session_id,))
            store._conn.commit()

        # 创建会话并添加消息
        store.create_session(test_session_id, Path.cwd())
        store.append_message(test_session_id, "user", [{"type": "text", "text": "What is 2+2?"}])
        store.append_message(
            test_session_id,
            "assistant",
            [{"type": "text", "text": "The answer is 4."}]
        )
        store.append_message(test_session_id, "user", [{"type": "text", "text": "What about 3+3?"}])

        print(f"  [OK] 会话 {test_session_id} 已创建")
        print(f"  [OK] 添加了 3 条消息")

        # 阶段 2: 验证消息已保存
        print("\n[阶段 2] 验证消息持久化...")
        messages = store.load_messages(test_session_id)
        print(f"  [OK] 加载了 {len(messages)} 条消息")

        for i, msg in enumerate(messages, 1):
            role = msg["role"]
            content_preview = str(msg["content"])[:50]
            print(f"    {i}. {role}: {content_preview}...")

        assert len(messages) == 3, f"Expected 3 messages, got {len(messages)}"

        # 阶段 3: 模拟 resume 逻辑
        print("\n[阶段 3] 测试 resume 参数...")

        # 这部分模拟 Agent 使用 resume_session_id 的行为
        print(f"  检查 session_exists('{test_session_id}'): {store.session_exists(test_session_id)}")

        if store.session_exists(test_session_id):
            restored_messages = store.load_messages(test_session_id)
            print(f"  [OK] 恢复了 {len(restored_messages)} 条历史消息")

            # 验证恢复的消息内容
            assert restored_messages[0]["role"] == "user"
            assert "2+2" in str(restored_messages[0]["content"])

            assert restored_messages[1]["role"] == "assistant"
            assert "4" in str(restored_messages[1]["content"])

            assert restored_messages[2]["role"] == "user"
            assert "3+3" in str(restored_messages[2]["content"])

            print("  [OK] 消息内容验证通过")

        # 阶段 4: 验证会话状态更新
        print("\n[阶段 4] 测试状态管理...")
        store.update_session_status(test_session_id, "running")
        sessions = store.list_sessions(status="running")
        found = any(s["id"] == test_session_id for s in sessions)
        assert found, f"Session {test_session_id} not found in running sessions"
        print(f"  [OK] 状态更新为 'running'")

        store.update_session_status(test_session_id, "completed")
        sessions = store.list_sessions(status="completed")
        found = any(s["id"] == test_session_id for s in sessions)
        assert found, f"Session {test_session_id} not found in completed sessions"
        print(f"  [OK] 状态更新为 'completed'")

        print("\n" + "="*70)
        print("[通过] Resume 功能集成测试通过")
        print("="*70)

        # 清理
        print("\n[清理] 删除测试会话...")
        store._conn.execute("DELETE FROM messages WHERE session_id = ?", (test_session_id,))
        store._conn.execute("DELETE FROM sessions WHERE id = ?", (test_session_id,))
        store._conn.commit()
        print("  [OK] 测试数据已清理")

    except Exception as e:
        print(f"\n[错误] {e}")
        raise
    finally:
        store.close()


async def test_resume_message_format():
    """测试消息格式兼容性"""
    print("\n" + "="*70)
    print("测试消息格式兼容性")
    print("="*70)

    from carbide.governance.session_store import SessionStore

    store_path = Path.cwd() / ".carbide" / "sessions.db"
    store = SessionStore(store_path)
    store.open()

    try:
        test_id = "format-test-session"

        # 清理
        if store.session_exists(test_id):
            store._conn.execute("DELETE FROM messages WHERE session_id = ?", (test_id,))
            store._conn.execute("DELETE FROM sessions WHERE id = ?", (test_id,))
            store._conn.commit()

        store.create_session(test_id, Path.cwd())

        # 测试不同格式的消息
        test_cases = [
            ("user", [{"type": "text", "text": "Simple text"}]),
            ("assistant", [{"type": "text", "text": "Response"}]),
            ("assistant", [
                {"type": "text", "text": "Text before tool"},
                {"type": "tool_use", "id": "tool_1", "name": "bash", "input": {"command": "ls"}},
            ]),
            ("user", [
                {"type": "tool_result", "tool_use_id": "tool_1", "content": "output"}
            ]),
        ]

        print("\n保存不同格式的消息...")
        for role, content in test_cases:
            store.append_message(test_id, role, content)
            print(f"  [OK] {role}: {len(content)} 个内容块")

        # 加载并验证
        print("\n加载消息...")
        messages = store.load_messages(test_id)
        print(f"  [OK] 加载了 {len(messages)} 条消息")

        assert len(messages) == len(test_cases)
        for i, (expected_role, expected_content) in enumerate(test_cases):
            msg = messages[i]
            assert msg["role"] == expected_role
            assert isinstance(msg["content"], list)
            print(f"  [OK] 消息 {i+1} 格式正确")

        # 清理
        store._conn.execute("DELETE FROM messages WHERE session_id = ?", (test_id,))
        store._conn.execute("DELETE FROM sessions WHERE id = ?", (test_id,))
        store._conn.commit()

        print("\n[通过] 消息格式兼容性测试通过")

    finally:
        store.close()


async def main():
    await test_resume_workflow()
    await test_resume_message_format()

    print("\n" + "="*70)
    print("所有 Resume 功能测试完成")
    print("="*70)


if __name__ == "__main__":
    asyncio.run(main())
