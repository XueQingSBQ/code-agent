"""
生产就绪测试: SessionStore持久化验证
测试会话保存、恢复、列表、清理功能
"""

import tempfile
import uuid
from pathlib import Path

from carbide.governance.session_store import SessionStore


class Results:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.total = 0

    def record(self, test_name: str, passed: bool, details: str = ""):
        self.total += 1
        if passed:
            self.passed.append((test_name, details))
            print(f"[PASS] {test_name}")
            if details:
                print(f"       {details}")
        else:
            self.failed.append((test_name, details))
            print(f"[FAIL] {test_name}")
            if details:
                print(f"       {details}")

    def summary(self):
        print("\n" + "=" * 70)
        print(f"测试结果: {len(self.passed)}/{self.total} 通过")
        print(f"通过率: {len(self.passed) / self.total * 100:.1f}%")
        if self.failed:
            print(f"\n失败的测试 ({len(self.failed)}):")
            for name, details in self.failed:
                print(f"  - {name}")
                if details:
                    print(f"    {details}")
        print("=" * 70)


def run_session_creation():
    """测试会话创建和保存"""
    print("\n" + "=" * 70)
    print("测试 1: SessionStore 会话创建")
    print("=" * 70)

    results = TestResults()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"

        # 测试 1.1: 数据库初始化
        try:
            store = SessionStore(db_path=db_path)
            store.open()
            store.close()

            db_exists = db_path.exists()
            results.record("1.1 数据库文件创建", db_exists, f"路径: {db_path}")
        except Exception as e:
            results.record("1.1 数据库文件创建", False, f"异常: {e}")

        # 测试 1.2: 创建新会话
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()

                store.create_session(session_id=session_id, workspace=workspace)

                exists = store.session_exists(session_id)
                results.record("1.2 创建新会话", exists, f"session_id: {session_id}")
        except Exception as e:
            results.record("1.2 创建新会话", False, f"异常: {e}")

        # 测试 1.3: 保存消息到会话
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                workspace = Path(tmpdir) / "workspace"

                store.create_session(session_id=session_id, workspace=workspace)

                # 添加消息
                store.append_message(
                    session_id=session_id,
                    role="user",
                    content=[{"type": "text", "text": "Hello, Claude!"}],
                )
                store.append_message(
                    session_id=session_id,
                    role="assistant",
                    content=[{"type": "text", "text": "Hello! How can I help you?"}],
                )

                messages = store.load_messages(session_id)
                results.record(
                    "1.3 保存消息到会话", len(messages) == 2, f"保存了 {len(messages)} 条消息"
                )
        except Exception as e:
            results.record("1.3 保存消息到会话", False, f"异常: {e}")

        # 测试 1.4: 获取会话信息
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                workspace = Path(tmpdir) / "workspace"

                store.create_session(session_id=session_id, workspace=workspace)

                info = store.get_session_info(session_id)

                is_valid = (
                    info is not None
                    and info["session_id"] == session_id
                    and info["status"] == "running"
                    and "created_at" in info
                )

                results.record(
                    "1.4 获取会话完整信息",
                    is_valid,
                    f"status={info['status']}, created_at={info['created_at'][:19]}",
                )
        except Exception as e:
            results.record("1.4 获取会话完整信息", False, f"异常: {e}")

        # 测试 1.5: 多条消息顺序
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                workspace = Path(tmpdir) / "workspace"

                store.create_session(session_id=session_id, workspace=workspace)

                messages_to_add = [
                    ("user", "Message 1"),
                    ("assistant", "Response 1"),
                    ("user", "Message 2"),
                    ("assistant", "Response 2"),
                    ("user", "Message 3"),
                ]

                for role, text in messages_to_add:
                    store.append_message(
                        session_id=session_id, role=role, content=[{"type": "text", "text": text}]
                    )

                loaded = store.load_messages(session_id)

                correct_order = all(
                    loaded[i]["content"][0]["text"] == text
                    for i, (_, text) in enumerate(messages_to_add)
                )

                results.record(
                    "1.5 多条消息顺序保持",
                    correct_order and len(loaded) == len(messages_to_add),
                    f"{len(loaded)} 条消息顺序正确",
                )
        except Exception as e:
            results.record("1.5 多条消息顺序保持", False, f"异常: {e}")

        # 测试 1.6: 会话状态更新
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                workspace = Path(tmpdir) / "workspace"

                store.create_session(session_id=session_id, workspace=workspace)

                # 更新状态
                store.update_session_status(session_id, "completed")

                info = store.get_session_info(session_id)
                results.record(
                    "1.6 会话状态更新",
                    info["status"] == "completed",
                    f"状态更新为: {info['status']}",
                )
        except Exception as e:
            results.record("1.6 会话状态更新", False, f"异常: {e}")

    results.summary()
    return results


def run_session_listing():
    """测试会话列表功能"""
    print("\n" + "=" * 70)
    print("测试 2: SessionStore 会话列表")
    print("=" * 70)

    results = TestResults()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"

        # 测试 2.1: 列出所有会话
        try:
            store = SessionStore(db_path=db_path)
            with store:
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()

                # 创建多个会话
                session_ids = []
                for i in range(5):
                    sid = str(uuid.uuid4())
                    store.create_session(session_id=sid, workspace=workspace)
                    store.append_message(
                        sid, "user", [{"type": "text", "text": f"Message in session {i}"}]
                    )
                    session_ids.append(sid)

                sessions = store.list_sessions()

                results.record(
                    "2.1 列出所有会话", len(sessions) == 5, f"创建5个会话, 列出{len(sessions)}个"
                )
        except Exception as e:
            results.record("2.1 列出所有会话", False, f"异常: {e}")

        # 测试 2.2: 会话按时间排序
        try:
            store = SessionStore(db_path=db_path)
            with store:
                sessions = store.list_sessions()

                # 检查是否按更新时间排序（最新的在前）
                if len(sessions) > 1:
                    timestamps = [s["updated_at"] for s in sessions]
                    is_sorted = all(
                        timestamps[i] >= timestamps[i + 1] for i in range(len(timestamps) - 1)
                    )
                else:
                    is_sorted = True

                results.record(
                    "2.2 会话按时间排序",
                    is_sorted,
                    f"{'按更新时间降序排列' if is_sorted else '排序不正确'}",
                )
        except Exception as e:
            results.record("2.2 会话按时间排序", False, f"异常: {e}")

        # 测试 2.3: 限制返回数量
        try:
            store = SessionStore(db_path=db_path)
            with store:
                sessions = store.list_sessions(limit=3)

                results.record(
                    "2.3 限制返回数量", len(sessions) == 3, f"限制3个，实际返回{len(sessions)}个"
                )
        except Exception as e:
            results.record("2.3 限制返回数量", False, f"异常: {e}")

        # 测试 2.4: 空数据库返回空列表
        try:
            empty_db = Path(tmpdir) / "empty.db"
            store = SessionStore(db_path=empty_db)
            with store:
                sessions = store.list_sessions()

                results.record(
                    "2.4 空数据库返回空列表", len(sessions) == 0, f"返回 {len(sessions)} 个会话"
                )
        except Exception as e:
            results.record("2.4 空数据库返回空列表", False, f"异常: {e}")

    results.summary()
    return results


def run_session_resume():
    """测试会话恢复功能"""
    print("\n" + "=" * 70)
    print("测试 3: SessionStore 会话恢复")
    print("=" * 70)

    results = TestResults()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        # 测试 3.1: 跨实例恢复会话
        try:
            session_id = str(uuid.uuid4())

            # 第一个实例：创建并保存
            store1 = SessionStore(db_path=db_path)
            with store1:
                store1.create_session(session_id=session_id, workspace=workspace)
                store1.append_message(
                    session_id, "user", [{"type": "text", "text": "Original message"}]
                )
                store1.append_message(
                    session_id, "assistant", [{"type": "text", "text": "Original response"}]
                )

            # 第二个实例：恢复
            store2 = SessionStore(db_path=db_path)
            with store2:
                exists = store2.session_exists(session_id)
                messages = store2.load_messages(session_id)

                is_valid = (
                    exists
                    and len(messages) == 2
                    and messages[0]["content"][0]["text"] == "Original message"
                )

                results.record(
                    "3.1 跨实例恢复会话", is_valid, f"恢复会话包含 {len(messages)} 条消息"
                )
        except Exception as e:
            results.record("3.1 跨实例恢复会话", False, f"异常: {e}")

        # 测试 3.2: 恢复后继续添加消息
        try:
            store = SessionStore(db_path=db_path)
            with store:
                # 添加新消息
                store.append_message(
                    session_id, "user", [{"type": "text", "text": "Continued message"}]
                )
                store.append_message(
                    session_id, "assistant", [{"type": "text", "text": "Continued response"}]
                )

                # 验证
                messages = store.load_messages(session_id)
                results.record(
                    "3.2 恢复后继续添加消息", len(messages) == 4, f"总共 {len(messages)} 条消息"
                )
        except Exception as e:
            results.record("3.2 恢复后继续添加消息", False, f"异常: {e}")

        # 测试 3.3: 消息内容完整性
        try:
            store = SessionStore(db_path=db_path)
            with store:
                test_session = str(uuid.uuid4())
                store.create_session(session_id=test_session, workspace=workspace)

                original_messages = [
                    ("user", "Test with 中文字符"),
                    ("assistant", "Response with emoji 😀"),
                    ("user", "Multi\nline\ncontent"),
                    ("assistant", '{"json": "content"}'),
                ]

                for role, text in original_messages:
                    store.append_message(test_session, role, [{"type": "text", "text": text}])

                loaded = store.load_messages(test_session)

                content_match = all(
                    loaded[i]["content"][0]["text"] == text
                    for i, (_, text) in enumerate(original_messages)
                )

                results.record(
                    "3.3 特殊字符内容完整性",
                    content_match and len(loaded) == len(original_messages),
                    "中文、emoji、换行、JSON格式保存正确",
                )
        except Exception as e:
            results.record("3.3 特殊字符内容完整性", False, f"异常: {e}")

        # 测试 3.4: 大量消息恢复
        try:
            store = SessionStore(db_path=db_path)
            with store:
                large_session = str(uuid.uuid4())
                store.create_session(session_id=large_session, workspace=workspace)

                num_messages = 100
                for i in range(num_messages):
                    role = "user" if i % 2 == 0 else "assistant"
                    store.append_message(
                        large_session, role, [{"type": "text", "text": f"Message {i}"}]
                    )

                loaded = store.load_messages(large_session)

                results.record(
                    "3.4 大量消息恢复",
                    len(loaded) == num_messages,
                    f"恢复 {len(loaded)}/{num_messages} 条消息",
                )
        except Exception as e:
            results.record("3.4 大量消息恢复", False, f"异常: {e}")

    results.summary()
    return results


def run_edge_cases():
    """测试边界情况"""
    print("\n" + "=" * 70)
    print("测试 4: SessionStore 边界情况")
    print("=" * 70)

    results = TestResults()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        # 测试 4.1: 获取不存在的会话
        try:
            store = SessionStore(db_path=db_path)
            with store:
                fake_id = str(uuid.uuid4())
                info = store.get_session_info(fake_id)

                results.record("4.1 获取不存在的会话", info is None, "返回None表示会话不存在")
        except Exception as e:
            results.record("4.1 获取不存在的会话", False, f"异常: {e}")

        # 测试 4.2: 加载不存在会话的消息
        try:
            store = SessionStore(db_path=db_path)
            with store:
                fake_id = str(uuid.uuid4())
                messages = store.load_messages(fake_id)

                results.record("4.2 加载不存在会话的消息", len(messages) == 0, "返回空列表")
        except Exception as e:
            results.record("4.2 加载不存在会话的消息", False, f"异常: {e}")

        # 测试 4.3: 重复创建相同session_id
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                store.create_session(session_id=session_id, workspace=workspace)

                # 尝试重复创建
                try:
                    store.create_session(session_id=session_id, workspace=workspace)
                    results.record("4.3 重复创建相同ID", False, "应该抛出异常")
                except Exception:
                    results.record("4.3 重复创建相同ID", True, "正确抛出异常")
        except Exception as e:
            # 外层异常是测试框架问题
            results.record("4.3 重复创建相同ID", False, f"测试异常: {e}")

        # 测试 4.4: 空消息内容
        try:
            store = SessionStore(db_path=db_path)
            with store:
                session_id = str(uuid.uuid4())
                store.create_session(session_id=session_id, workspace=workspace)

                store.append_message(session_id, "user", [])

                messages = store.load_messages(session_id)

                results.record(
                    "4.4 空消息内容",
                    len(messages) == 1 and messages[0]["content"] == [],
                    "空content列表正确保存",
                )
        except Exception as e:
            results.record("4.4 空消息内容", False, f"异常: {e}")

        # 测试 4.5: WAL模式验证
        try:
            store = SessionStore(db_path=db_path)
            with store:
                # 检查WAL模式是否启用
                cursor = store._conn.execute("PRAGMA journal_mode")
                mode = cursor.fetchone()[0]

                results.record("4.5 WAL模式启用", mode.lower() == "wal", f"journal_mode={mode}")
        except Exception as e:
            results.record("4.5 WAL模式启用", False, f"异常: {e}")

    results.summary()
    return results


def main():
    """运行所有SessionStore测试"""
    print("\n" + "=" * 70)
    print("生产就绪测试: 1.2 SessionStore持久化验证")
    print("=" * 70)

    all_results = []

    # 运行所有测试
    all_results.append(run_session_creation())
    all_results.append(run_session_listing())
    all_results.append(run_session_resume())
    all_results.append(run_edge_cases())

    # 总体统计
    total_passed = sum(len(r.passed) for r in all_results)
    total_tests = sum(r.total for r in all_results)

    print("\n" + "=" * 70)
    print("总体结果")
    print("=" * 70)
    print(f"总测试数: {total_tests}")
    print(f"通过: {total_passed}")
    print(f"失败: {total_tests - total_passed}")
    print(f"通过率: {total_passed / total_tests * 100:.1f}%")
    print("=" * 70)

    # 判断是否达到生产标准
    pass_rate = total_passed / total_tests
    if pass_rate >= 0.9:
        print("\n[通过] SessionStore持久化验证达到生产标准 (>=90%)")
        return 0
    elif pass_rate >= 0.75:
        print("\n[警告] SessionStore持久化验证基本达标但需改进 (75-90%)")
        return 1
    else:
        print("\n[失败] SessionStore持久化验证未达到生产标准 (<75%)")
        return 2


if __name__ == "__main__":
    exit(main())
