# M1: Persistent Bash Session Implementation

## 概述

实现了持久化 Bash 会话功能，使得跨多个命令执行时能够保持状态（当前目录、环境变量、shell 函数等）。

## 核心组件

### 1. BashSession (`src/carbide/tools/session.py`)

长生命周期的 bash 子进程，在多次命令执行之间保持状态。

**关键设计决策：**
- 使用唯一标记（UUID 前缀）可靠地分隔多个命令的输出
- 通过 `echo $?` 在每个命令后捕获退出码
- 异步读取任务防止大输出时死锁
- 超时针对每个命令，而非整个会话
- Windows：显式使用 Git Bash 避免 WSL bash 包装器

**平台特定处理：**
- Windows 上多个 bash.exe 存在（Git Bash、WSL、w64devkit）
- WSL bash 输出 UTF-16LE 编码
- `_find_bash()` 函数优先选择 Git Bash，按以下顺序回退：
  1. `C:\Program Files\Git\usr\bin\bash.exe`
  2. `E:\w64devkit\bin\bash.exe`
  3. `C:\msys64\usr\bin\bash.exe`
  4. `shutil.which("bash")` (避开 System32 和 WindowsApps)

**API：**
```python
async with BashSession(workspace=Path.cwd()) as session:
    output, code = await session.execute("echo hello")
    output, code = await session.execute("pwd")
```

### 2. PersistentBashTool (`src/carbide/tools/persistent_bash.py`)

使用 BashSession 的工具实现，集成到工具注册表中。

**特性：**
- 在调用之间维护会话状态
- 每个命令支持自定义超时
- 大输出自动截断（30KB 限制）
- 会话级别锁保证命令顺序执行
- 显式会话清理支持

**工具属性：**
- `read_only`: False
- `destructive`: True
- `concurrency_safe`: False（同一会话中的命令必须顺序执行）

### 3. 集成到 SDK (`src/carbide/sdk.py`)

PersistentBashTool 已注册到默认工具池中。

## 测试覆盖

### BashSession 测试 (`tests/test_bash_session.py`)
- ✅ 基本命令执行
- ✅ 退出码捕获（成功和失败）
- ✅ 跨命令状态保持（目录、环境变量）
- ✅ stderr 合并到输出
- ✅ 大输出不阻塞
- ✅ 超时不终止会话
- ✅ 多行命令
- ✅ 引号处理
- ✅ 并发会话隔离
- ✅ 工作目录正确设置
- ✅ 失败命令包含错误信息

### PersistentBashTool 测试 (`tests/test_persistent_bash_tool.py`)
- ✅ 基本命令执行
- ✅ 退出码捕获
- ✅ 跨调用状态持久化
- ✅ 超时处理
- ✅ 大输出截断
- ✅ 会话跨调用重用
- ✅ 会话清理

**测试结果：** 所有 19 个测试通过

## 技术细节

### 标记协议

每个命令被包装为：
```bash
printf "%s\n" "__CARBIDE_<uuid>__START"
<用户命令>
__exit_code=$?
printf "\n%s\n" "__CARBIDE_<uuid>__EXIT$__exit_code"
printf "%s\n" "__CARBIDE_<uuid>__END"
```

解析器在标记之间提取实际输出和退出码。

### Windows Bash 歧义问题

**问题：** Windows 系统上可能存在多个 bash.exe：
- WSL bash (`C:\Windows\System32\bash.exe`) - 输出 UTF-16LE
- Git Bash (`C:\Program Files\Git\usr\bin\bash.exe`) - 输出 UTF-8
- w64devkit bash - 输出 UTF-8
- MSYS2 bash - 输出 UTF-8

**解决方案：** 显式路径解析优先选择 Git Bash，避免 WSL 包装器。

### 异步队列模式

后台读取任务将行推送到 `asyncio.Queue`：
```python
async def _read_loop(self) -> None:
    while True:
        line_bytes = await self._proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace")
        await self._stdout_queue.put(line)
```

命令执行从队列中消费直到看到结束标记，防止大输出死锁。

## 已知限制

1. **超时行为：** 超时不会终止正在运行的命令，只是停止等待输出。`sleep` 进程可能继续运行。
2. **会话清理：** 工具实例持有会话，需要显式调用 `close_session()` 来清理。
3. **编码假设：** 假设 UTF-8 输出（通过 `LC_ALL=C.UTF-8` 强制）。

## 下一步

M1 的持久化 Bash 会话功能已完成并测试通过。可以继续 M1 的其他工具层改进，或者开始后续里程碑的工作。
