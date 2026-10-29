# M1 工具层完成报告

## 概述
M1 里程碑现已**100%完成** ✅

## 实现的核心组件

### 1. 持久化 BashSession ⭐ (新增)
**文件**: `src/carbide/tools/bash_session.py`

**功能**:
- 维护长期运行的bash进程
- 保留跨命令状态：
  - ✅ 工作目录 (cd)
  - ✅ 环境变量 (export)
  - ✅ Shell函数
- 进程隔离：每个(workspace, shell)组合独立session
- 退出码捕获
- 超时处理和进程恢复
- 输出截断（30KB限制）

**实现细节**:
```python
class BashSession:
    - start(): 启动持久bash进程
    - execute(command, timeout): 执行命令并返回(output, exit_code)
    - close(): 清理进程
    - 使用唯一delimiter标记命令输出边界
    - 使用asyncio.Lock确保命令串行执行
```

**跨平台支持**:
- Linux/macOS: `/bin/bash`
- Windows Git Bash: 自动检测多个位置
- WSL: `wsl bash`

### 2. BashTool集成
**文件**: `src/carbide/tools/shell.py` (已更新)

**变更**:
- 导入BashSession
- 添加类级别session缓存: `_sessions: dict[tuple[str, str], BashSession]`
- `_get_session()`: 获取或创建session
- `run()`: 使用持久session替代一次性进程

**优势**:
```python
# 旧方案 (M0): 每次创建新进程
proc = await asyncio.create_subprocess_exec(...)

# 新方案 (M1): 复用持久session
session = self._get_session(ctx)
output, exit_code = await session.execute(command)
```

## 测试覆盖

### 单元测试: `test_bash_session.py`
✅ 7/7 通过
- 基础命令执行
- cd状态持久化
- 环境变量持久化
- 函数定义持久化
- 退出码捕获
- 多命令序列
- 超时处理

### 集成测试: `test_shell_tool_persistence.py`
✅ 3/3 通过
- BashTool状态持久化（env、cd、函数）
- Session隔离（不同workspace独立）
- 错误处理

## 对比 M0

| 特性 | M0 | M1 |
|------|----|----|
| 执行方式 | 每次新进程 | 持久session |
| cd命令 | ❌ 不保留 | ✅ 保留 |
| 环境变量 | ❌ 不保留 | ✅ 保留 |
| Shell函数 | ❌ 不保留 | ✅ 保留 |
| 调试体验 | 差 | 好 |
| 性能 | 启动开销大 | 复用进程快 |

## 使用示例

```python
from carbide.tools.shell import BashTool, BashParams
from carbide.tools.base import ToolContext

tool = BashTool()
ctx = ToolContext(workspace="/path/to/project", shell="bash")

# 设置环境变量
await tool.run(BashParams(command="export DB_URL='postgres://...'"), ctx)

# 后续命令可以访问
result = await tool.run(BashParams(command="echo $DB_URL"), ctx)
# 输出: postgres://...

# 改变目录
await tool.run(BashParams(command="cd src"), ctx)
await tool.run(BashParams(command="pwd"), ctx)
# 输出: /path/to/project/src
```

## M1 完整清单

### ✅ 已完成
- [x] Tool ABC 基类和契约 (M0)
- [x] ToolRegistry (M0)
- [x] Grep/Glob 工具 (M0)
- [x] 并发执行器 (M0)
- [x] **持久化 BashSession** (M1 核心交付物) ⭐

### 🎯 M1 目标达成
所有M1计划要求已实现，包括最关键的**持久化Bash会话**功能。

## 下一步：M2 治理层

缺失组件：
1. **SQLite存储** - 会话状态持久化
2. **--resume功能** - 恢复中断的会话
3. **Telemetry** - 系统可观测性

优先级：SQLite存储 + --resume（M2核心交付物）

## 总结

✅ **M1 已100%完成**
- 持久化BashSession实现并测试通过
- 完整集成到BashTool
- 支持cd、env vars、functions状态保留
- 跨平台支持（Windows/Linux/macOS）
- 7个单元测试 + 3个集成测试全部通过

这是M1的核心交付物，显著提升了多轮shell交互的用户体验。
