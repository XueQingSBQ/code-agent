"""
端到端测试 list-sessions CLI 命令
"""

import asyncio
import subprocess
from pathlib import Path


async def create_test_sessions():
    """创建几个测试会话"""
    print("创建测试会话...")

    from carbide.governance.session_store import SessionStore

    store_path = Path.cwd() / ".carbide" / "sessions.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)

    store = SessionStore(store_path)
    store.open()

    try:
        # 清理旧测试会话
        for session_id in ["test-session-1", "test-session-2", "test-session-3"]:
            if store.session_exists(session_id):
                store._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                store._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        store._conn.commit()

        # 创建 3 个会话
        store.create_session("test-session-1", Path.cwd())
        store.update_session_status("test-session-1", "completed")
        store.append_message("test-session-1", "user", [{"type": "text", "text": "Hello"}])
        store.append_message("test-session-1", "assistant", [{"type": "text", "text": "Hi"}])

        store.create_session("test-session-2", Path.cwd())
        store.update_session_status("test-session-2", "running")
        store.append_message("test-session-2", "user", [{"type": "text", "text": "Test"}])

        store.create_session("test-session-3", Path.cwd())
        store.update_session_status("test-session-3", "failed")
        store.append_message("test-session-3", "user", [{"type": "text", "text": "Error"}])
        store.append_message("test-session-3", "assistant", [{"type": "text", "text": "Failed"}])
        store.append_message("test-session-3", "user", [{"type": "text", "text": "Retry"}])

        print(f"[OK] 创建了 3 个测试会话")
        print(f"  数据库路径: {store_path}")

    finally:
        store.close()


def test_list_sessions_command():
    """测试 list-sessions 命令"""
    print("\n测试 CLI 命令...")

    test_cases = [
        ("carbide list-sessions", "所有会话"),
        ("carbide list-sessions --status completed", "已完成会话"),
        ("carbide list-sessions --status running", "运行中会话"),
        ("carbide list-sessions --status failed", "失败会话"),
    ]

    for cmd, desc in test_cases:
        print(f"\n[测试] {desc}: {cmd}")
        result = subprocess.run(
            cmd.split(),
            capture_output=True,
            text=True,
            encoding='utf-8'
        )

        print(f"返回码: {result.returncode}")

        if result.stdout:
            print("输出:")
            print(result.stdout)

        if result.stderr:
            print("错误:")
            print(result.stderr)


async def main():
    print("="*70)
    print("端到端测试: list-sessions CLI 命令")
    print("="*70)

    # 创建测试数据
    await create_test_sessions()

    # 测试 CLI 命令
    test_list_sessions_command()

    print("\n" + "="*70)
    print("测试完成")
    print("="*70)


if __name__ == "__main__":
    asyncio.run(main())
