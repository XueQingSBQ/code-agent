"""
测试 SessionStore API 补充和 list-sessions 命令
"""

import tempfile
from pathlib import Path


def test_session_store_api():
    """测试 SessionStore 的补充方法"""
    print("\n" + "="*70)
    print("测试 SessionStore API 补充")
    print("="*70)

    from carbide.governance.session_store import SessionStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"
        store = SessionStore(db_path)
        store.open()

        try:
            # 创建测试会话
            print("\n[1] 创建测试会话...")
            store.create_session("session-1", Path("/workspace1"))
            store.update_session_status("session-1", "completed")

            store.create_session("session-2", Path("/workspace2"))
            store.update_session_status("session-2", "running")

            store.create_session("session-3", Path("/workspace3"))
            store.update_session_status("session-3", "failed")

            print("   [OK] 创建了 3 个会话")

            # 添加消息
            print("\n[2] 添加消息...")
            store.append_message("session-1", "user", [{"type": "text", "text": "msg1"}])
            store.append_message("session-1", "assistant", [{"type": "text", "text": "reply1"}])
            store.append_message("session-2", "user", [{"type": "text", "text": "msg2"}])
            store.append_message("session-3", "user", [{"type": "text", "text": "msg3"}])
            store.append_message("session-3", "assistant", [{"type": "text", "text": "reply3"}])
            store.append_message("session-3", "user", [{"type": "text", "text": "msg4"}])
            print("   [OK] 添加了多条消息")

            # 测试 list_sessions (无过滤)
            print("\n[3] 测试 list_sessions() - 无过滤...")
            all_sessions = store.list_sessions()
            print(f"   [OK] 返回 {len(all_sessions)} 个会话")
            assert len(all_sessions) == 3, f"Expected 3 sessions, got {len(all_sessions)}"

            # 测试 list_sessions (按状态过滤)
            print("\n[4] 测试 list_sessions(status='completed')...")
            completed = store.list_sessions(status="completed")
            print(f"   [OK] 返回 {len(completed)} 个已完成会话")
            assert len(completed) == 1, f"Expected 1 completed session, got {len(completed)}"
            assert completed[0]["status"] == "completed"

            print("\n[5] 测试 list_sessions(status='running')...")
            running = store.list_sessions(status="running")
            print(f"   [OK] 返回 {len(running)} 个运行中会话")
            assert len(running) == 1, f"Expected 1 running session, got {len(running)}"

            print("\n[6] 测试 list_sessions(status='failed')...")
            failed = store.list_sessions(status="failed")
            print(f"   [OK] 返回 {len(failed)} 个失败会话")
            assert len(failed) == 1, f"Expected 1 failed session, got {len(failed)}"

            # 测试 count_messages
            print("\n[7] 测试 count_messages()...")
            count1 = store.count_messages("session-1")
            count2 = store.count_messages("session-2")
            count3 = store.count_messages("session-3")
            print(f"   session-1: {count1} 条消息")
            print(f"   session-2: {count2} 条消息")
            print(f"   session-3: {count3} 条消息")
            assert count1 == 2, f"Expected 2 messages in session-1, got {count1}"
            assert count2 == 1, f"Expected 1 message in session-2, got {count2}"
            assert count3 == 3, f"Expected 3 messages in session-3, got {count3}"
            print("   [OK] 消息计数正确")

            # 测试 session_exists
            print("\n[8] 测试 session_exists()...")
            assert store.session_exists("session-1") == True
            assert store.session_exists("non-existent") == False
            print("   [OK] session_exists 工作正常")

            print("\n" + "="*70)
            print("[通过] 所有 SessionStore API 测试通过")
            print("="*70)

        finally:
            store.close()


def test_list_sessions_output():
    """测试 list_sessions 的输出格式"""
    print("\n" + "="*70)
    print("测试 list_sessions 输出格式")
    print("="*70)

    from carbide.governance.session_store import SessionStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"
        store = SessionStore(db_path)
        store.open()

        try:
            # 创建会话
            store.create_session("abc123", Path("/test/workspace"))
            store.update_session_status("abc123", "completed")
            store.append_message("abc123", "user", [{"type": "text", "text": "test"}])

            # 获取会话列表
            sessions = store.list_sessions()

            print("\n会话列表格式:")
            for session in sessions:
                print(f"  ID: {session['id']}")
                print(f"  Status: {session['status']}")
                print(f"  Created: {session['created_at']}")
                print(f"  Updated: {session['updated_at']}")
                print(f"  Workspace: {session['workspace']}")

                # 验证必需字段存在
                assert 'id' in session
                assert 'status' in session
                assert 'created_at' in session
                assert 'updated_at' in session
                assert 'workspace' in session

            print("\n[OK] 输出格式正确")

        finally:
            store.close()


if __name__ == "__main__":
    test_session_store_api()
    test_list_sessions_output()
    print("\n" + "="*70)
    print("所有测试完成")
    print("="*70)
