# Carbide Session 管理快速指南

## 会话持久化和恢复

### 自动会话持久化

Carbide 现在会自动保存每个会话的所有消息和状态到 `.carbide/sessions.db`：

```bash
# 正常使用 - 会话自动保存
carbide "帮我修复测试"

# 会话数据自动存储在:
# .carbide/sessions.db
```

**保存内容**:
- 用户消息
- 助手响应
- 工具执行结果
- 会话状态（running/completed/failed）
- 访问的文件列表

---

## 查看会话列表

### 列出所有会话

```bash
carbide list-sessions
```

**输出示例**:
```
Sessions in E:\claude:

ID                                       Status       Created              Messages
-------------------------------------------------------------------------------
test-session-3                           failed       2026-09-17T09:14:57  3
test-session-2                           running      2026-09-17T09:14:57  1
test-session-1                           completed    2026-09-17T09:14:57  2

Total: 3 session(s)
```

### 按状态过滤

```bash
# 只显示运行中的会话
carbide list-sessions --status running

# 只显示已完成的会话
carbide list-sessions --status completed

# 只显示失败的会话
carbide list-sessions --status failed
```

---

## 恢复会话

### 继续之前的对话

```bash
# 从 list-sessions 获取会话 ID
carbide list-sessions

# 使用 --resume 恢复会话
carbide --resume <session-id>
```

**示例工作流**:
```bash
# 1. 开始一个任务
carbide "分析这个项目的架构"
# ... 工作一段时间后退出

# 2. 查看会话 ID
carbide list-sessions

# 3. 恢复会话继续工作
carbide --resume abc123-def456-...

# 4. 在交互模式中继续对话
> 现在帮我实现 API 层
```

**恢复时的行为**:
- ✅ 加载所有历史消息
- ✅ 恢复对话上下文
- ✅ 保持工作目录
- ✅ 继续使用相同的 session_id

---

## 会话状态

### 状态类型

| 状态 | 说明 | 何时出现 |
|------|------|---------|
| `running` | 会话正在运行或未正常退出 | Agent 启动后，退出前 |
| `completed` | 会话正常完成 | Agent 正常退出（无异常） |
| `failed` | 会话因错误终止 | Agent 退出时有未处理的异常 |

### 状态自动更新

状态在 Agent 退出时自动更新，无需手动管理：

```python
# Agent 正常完成
async with Agent(...) as agent:
    await agent.query("...")
# → 自动更新为 "completed"

# Agent 遇到错误
async with Agent(...) as agent:
    raise Exception("Something went wrong")
# → 自动更新为 "failed"
```

---

## 资源清理

### 自动清理机制

当 Agent 退出时，自动清理所有资源：

1. **BashSession 进程**
   - 关闭所有打开的 bash 会话
   - 终止后台进程
   - 清理临时文件

2. **Asyncio Tasks**
   - 取消所有未完成的任务
   - 等待清理完成
   - 防止资源泄漏

3. **会话状态**
   - 更新最终状态
   - 记录完成时间
   - 持久化到数据库

**无需手动干预** - 使用 `async with` 即可：

```python
async with Agent(provider, tools) as agent:
    # 工作...
    pass
# 退出时自动清理一切
```

---

## 文件访问跟踪

### 自动跟踪访问的文件

系统自动跟踪通过 Read/Edit/Write 工具访问的文件：

```python
# 这些操作会被自动跟踪
Read(file_path="src/main.py")
Edit(file_path="src/utils.py", ...)
Write(file_path="tests/test_api.py", ...)

# 最近 20 个文件保存在 state.active_files
# 用于智能上下文压缩
```

**用途**:
- 上下文压缩时优先保留活跃文件
- 恢复会话时恢复相关文件
- 性能优化（L4.5 压缩层）

---

## 使用场景

### 场景 1: 长时间任务

```bash
# 开始一个大型重构任务
carbide "重构认证模块"

# 工作 30 分钟后需要离开
# 按 Ctrl+C 或正常退出

# 稍后返回
carbide list-sessions --status running
carbide --resume <session-id>

# 继续工作
> 现在添加测试
```

### 场景 2: 错误恢复

```bash
# 会话因网络问题失败
carbide "生成 API 文档"
# Error: API rate limit...

# 查看失败的会话
carbide list-sessions --status failed

# 恢复并重试
carbide --resume <session-id>
> 重新生成文档
```

### 场景 3: 会话管理

```bash
# 查看所有活跃项目
carbide list-sessions --status running

# 查看完成的任务历史
carbide list-sessions --status completed

# 清理或存档旧会话
# (未来功能: carbide archive-session <id>)
```

---

## 数据存储

### 数据库位置

```
项目根目录/
  .carbide/
    sessions.db    # SQLite 数据库
```

### 数据库结构

**sessions 表**:
- `id`: 会话唯一标识
- `created_at`: 创建时间
- `updated_at`: 最后更新时间
- `workspace`: 工作目录
- `status`: 会话状态

**messages 表**:
- `session_id`: 所属会话
- `role`: user/assistant
- `content`: JSON 格式的消息内容
- `timestamp`: 消息时间

### 数据库管理

```bash
# 查看数据库（使用 sqlite3）
sqlite3 .carbide/sessions.db

# 查询会话
SELECT id, status, created_at FROM sessions;

# 查询消息数量
SELECT session_id, COUNT(*) FROM messages GROUP BY session_id;
```

---

## 最佳实践

### ✅ 推荐做法

1. **使用描述性的初始提示**
   ```bash
   carbide "项目: 用户认证模块 - 添加 OAuth 支持"
   ```

2. **定期查看会话列表**
   ```bash
   carbide list-sessions
   ```

3. **及时恢复未完成的工作**
   ```bash
   carbide list-sessions --status running
   carbide --resume <session-id>
   ```

### ❌ 避免

1. **不要手动编辑数据库**
   - 可能破坏数据完整性
   - 使用 CLI 命令管理

2. **不要依赖未提交的会话状态**
   - 总是让任务完成或显式保存
   - 异常退出可能丢失最后的未保存状态

---

## 故障排除

### 问题：恢复会话失败

```bash
carbide --resume <session-id>
# Error: Session not found
```

**解决方案**:
1. 检查会话 ID 是否正确
   ```bash
   carbide list-sessions
   ```

2. 确认数据库文件存在
   ```bash
   ls .carbide/sessions.db
   ```

3. 检查数据库权限
   ```bash
   sqlite3 .carbide/sessions.db "SELECT COUNT(*) FROM sessions;"
   ```

### 问题：会话状态显示 "running" 但实际已退出

**原因**: Agent 异常退出未能更新状态

**影响**: 仅显示问题，不影响恢复功能

**解决**: 恢复会话时状态会自动更正

---

## 未来功能（计划中）

- 🔄 会话归档和清理
- 📊 会话统计和分析
- 🔍 搜索会话内容
- 📤 导出会话记录
- 🏷️ 会话标签和分类

---

## 相关文档

- [P0-P3 完成报告](P0_P3_COMPLETION_REPORT.md)
- [修复总结](FIXES_SUMMARY.md)
- [API 文档](docs/)

---

**版本**: v1.0 (2026-09-17)  
**状态**: ✅ 生产就绪
