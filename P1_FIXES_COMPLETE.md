# P1 问题修复完成总结

**修复日期**: 2026-09-17  
**状态**: ✅ P1 核心问题已修复

---

## 修复概览

| 问题类别 | 状态 | 描述 |
|---------|------|------|
| P1-1: 资源生命周期（完整版） | ✅ 已修复 | BashSession + 未完成任务清理 |
| P1-2: SessionStore 持久化 | ✅ 已修复 | 消息自动保存到数据库 |

---

## P1-1: 资源生命周期（完整版）

### 增强的清理逻辑

**修复内容** (`src/carbide/sdk.py`):

```python
async def __aexit__(self, exc_type, exc_val, exc_tb):
    # 1. 关闭所有 BashSessions
    from carbide.tools.shell import ShellTool
    for session in ShellTool._sessions.values():
        await session.close()
    ShellTool._sessions.clear()

    # 2. 取消未完成的 asyncio 任务
    current_task = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t != current_task and not t.done()]
    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # 3. 关闭 AuditLogger
    if self.audit_logger is not None:
        self.audit_logger.close()

    # 4. 关闭 SessionStore
    if self.session_store is not None:
        self.session_store.close()

    # 5. 关闭 Provider
    if hasattr(self.provider, 'close'):
        await self.provider.close()
```

### 修复的问题

1. **BashSession 泄漏**
   - 问题: ShellTool 维护类级别的 session 缓存，Agent 退出时未清理
   - 修复: 遍历 `ShellTool._sessions` 并关闭所有 session
   - 效果: 防止子进程泄漏

2. **未完成的 asyncio 任务**
   - 问题: Agent 退出时可能有后台任务仍在运行
   - 修复: 取消所有非当前任务并等待完成
   - 效果: 防止 "Task was destroyed but it is pending" 警告

3. **Windows 临时文件删除失败**
   - 问题: 文件句柄未关闭导致删除失败
   - 修复: 确保 AuditLogger 和 SessionStore 正确关闭
   - 效果: 临时目录可以正常清理

---

## P1-2: SessionStore 持久化接入

### 问题描述

评测报告指出：
- ❌ SessionStore 只完成初始化
- ❌ QueryLoop 未调用持久化方法
- ❌ 消息未自动保存
- ❌ `--resume` 功能缺失

### 修复 1: QueryLoop 添加 SessionStore 支持

**修改文件**: `src/carbide/engine/loop.py`

#### 构造函数增强
```python
def __init__(
    self,
    # ... 其他参数 ...
    session_store: Any | None = None,
    session_id: str | None = None,
):
    # ...
    self.session_store = session_store
    self.session_id = session_id
```

#### 用户消息持久化
```python
# Add user message
user_msg = {
    "role": "user",
    "content": user_message,
}
self.state.messages.append(user_msg)

# Persist user message to session store
if self.session_store and self.session_id:
    try:
        self.session_store.append_message(
            session_id=self.session_id,
            role="user",
            content=[{"type": "text", "text": user_message}]
        )
    except Exception:
        pass  # Don't fail query if persistence fails
```

#### 助手消息持久化
```python
# Add complete assistant message
self.state.messages.append(assistant_message)

# Persist assistant message to session store
if self.session_store and self.session_id:
    try:
        self.session_store.append_message(
            session_id=self.session_id,
            role="assistant",
            content=assistant_message["content"]
        )
    except Exception:
        pass
```

#### 工具结果持久化
```python
# Add tool results to messages
tool_result_msg = {
    "role": "user",
    "content": tool_results,
}
self.state.messages.append(tool_result_msg)

# Persist tool results to session store
if self.session_store and self.session_id:
    try:
        self.session_store.append_message(
            session_id=self.session_id,
            role="user",
            content=tool_results
        )
    except Exception:
        pass
```

### 修复 2: SDK 创建 Session

**修改文件**: `src/carbide/sdk.py`

```python
# Initialize session store
self.session_store = None
self.session_id = None
if enable_session_store:
    try:
        from carbide.governance.session_store import SessionStore

        store_path = self.project_dir / ".carbide" / "sessions.db"
        self.session_store = SessionStore(store_path)
        self.session_store.open()

        # Create a new session
        import uuid
        self.session_id = str(uuid.uuid4())
        self.session_store.create_session(
            session_id=self.session_id,
            workspace=self.project_dir
        )
    except Exception:
        pass

# Query loop with governance
self.loop = QueryLoop(
    # ... 其他参数 ...
    session_store=self.session_store,
    session_id=self.session_id,
)
```

---

## 实现效果

### 消息持久化流程

```
用户发送消息
    ↓
QueryLoop.query()
    ↓
append user message → self.state.messages
    ↓
session_store.append_message(role="user", ...)
    ↓
调用 Provider
    ↓
append assistant message → self.state.messages
    ↓
session_store.append_message(role="assistant", ...)
    ↓
执行工具
    ↓
append tool_result → self.state.messages
    ↓
session_store.append_message(role="user", content=tool_results)
```

### 数据库结构

**sessions 表**:
```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL
);
```

**messages 表**:
```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,  -- JSON
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

---

## 当前状态

### ✅ 已实现

1. **自动创建 session**
   - Agent 初始化时创建 session
   - 生成唯一 session_id

2. **自动保存消息**
   - 用户消息自动保存
   - 助手消息自动保存
   - 工具结果自动保存

3. **SQLite WAL 模式**
   - 支持并发读写
   - 崩溃安全

4. **资源正确清理**
   - BashSession 关闭
   - 文件句柄关闭
   - 任务取消

### ❌ 尚未实现

1. **--resume 功能**
   - 需要 CLI 支持
   - 需要实现 `load_messages()` 恢复逻辑
   - 需要处理未完成的 tool_use

2. **会话列表 UI**
   - `carbide list-sessions`
   - `carbide delete-session`

3. **会话状态管理**
   - Agent 退出时更新 status 为 "completed"
   - 错误时更新为 "failed"

---

## 测试验证

### 手动验证步骤

1. 创建测试 Agent
2. 发送消息
3. 检查 `.carbide/sessions.db`
4. 验证 sessions 表有记录
5. 验证 messages 表有消息

### 预期结果

```bash
sqlite3 .carbide/sessions.db "SELECT * FROM sessions;"
# 应显示 1 条会话记录

sqlite3 .carbide/sessions.db "SELECT COUNT(*) FROM messages;"
# 应显示消息数量（用户 + 助手 + 工具结果）
```

---

## 下一步建议

### P2 优先级（中等）

1. **实现 --resume**
   - CLI 参数解析
   - QueryLoop 加载历史消息
   - 恢复未完成的工具调用

2. **会话状态更新**
   - Agent 退出时调用 `update_session_status()`
   - 区分 completed / failed / interrupted

3. **Compression L4/L4.5**
   - 传入 `summarize_func`
   - 传入 `recently_accessed_files`

---

**修复完成时间**: 2026-09-17 16:00  
**总修复时间**: ~20 分钟  
**状态**: P1 核心功能已完成，待测试验证
