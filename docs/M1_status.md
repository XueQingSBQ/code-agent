# M1 工具层 - 完成状态检查

## 计划范围（第 255 行）
> **M1 工具层** | Tool ABC 四道契约防线、Pydantic schema、Grep(ripgrep)/Glob、持久 Bash session、StreamingToolExecutor 并发

## 完成情况

### ✅ 已完成

1. **Tool ABC 四道契约防线**
   - `base.py` 中的 `Tool` 抽象基类 ✅
   - `__init_subclass__` 导入期校验 ✅
   - 注册时二次断言（`registry.py`）✅
   - 测试覆盖（`test_tool_contract.py`，7 个测试）✅

2. **Pydantic Schema**
   - 所有工具使用 Pydantic `BaseModel` 定义参数 ✅
   - 运行时参数校验 ✅

3. **Grep 工具**
   - `GrepTool` 实现（`search.py`）✅
   - ripgrep 快速路径 + Python 回退 ✅
   - 三种输出模式：`files_with_matches`（默认）/ `content` / `count` ✅
   - 测试覆盖（`test_tools.py` 中有 grep 相关测试）✅

4. **Glob 工具**
   - `GlobTool` 实现（`search.py`）✅
   - 按 mtime 倒序排序 ✅
   - 硬上限 100 个文件 ✅
   - 测试覆盖 ✅

5. **持久 Bash Session**
   - `BashSession` 类（`session.py`）✅
   - `PersistentBashTool` 工具封装（`persistent_bash.py`）✅
   - 平台特定 bash 检测（Windows/Linux）✅
   - 完整测试覆盖（19 个测试全部通过）✅

6. **StreamingToolExecutor**
   - `executor.py` 核心实现 ✅
   - 流式并行调度（边解析边派发）✅
   - `concurrency_safe` 属性控制并发 ✅
   - 安全工具并行，不安全工具作为屏障 ✅
   - **注意：使用 asyncio.Task 而非 TaskGroup**（正确设计：不使用 TaskGroup 的自动取消）✅
   - 测试覆盖（`test_executor.py`，5 个测试）✅

### ❌ 计划中但不在 M1 范围

以下是计划第 170 行提到的，但可能属于后续里程碑：

- **TodoWrite 工具** - 计划提到但未明确属于 M1
- **Task/Explore 子 agent** - 计划第 179 行提到，可能是 M1 范围外
- **snip 工具** - 属于 context 层（M3），不是 M1

## 核心工具清单

| 工具 | 文件 | read_only | destructive | concurrency_safe | 测试 |
|------|------|-----------|-------------|------------------|------|
| ReadTool | fs.py | ✓ | ✗ | ✓ | ✓ |
| WriteTool | fs.py | ✗ | ✓ | ✗ | ✓ |
| EditTool | fs.py | ✗ | ✓ | ✗ | ✓ |
| GlobTool | search.py | ✓ | ✗ | ✓ | ✓ |
| GrepTool | search.py | ✓ | ✗ | ✓ | ✓ |
| BashTool | shell.py | ✗ | ✓ | ✗ | ✓ |
| PersistentBashTool | persistent_bash.py | ✗ | ✓ | ✗ | ✓ |

## 测试结果

```
tests/test_tools.py: 56 passed, 14 skipped
tests/test_executor.py: 5 passed
tests/test_tool_contract.py: 7 passed
tests/test_bash_session.py: 12 passed
tests/test_persistent_bash_tool.py: 7 passed

总计: 87 个测试通过
```

## 结论

**M1 工具层已经 100% 完成！**

所有计划中的核心组件都已实现并经过测试：
- ✅ Tool ABC 契约机制
- ✅ Pydantic schema
- ✅ Grep/Glob 工具
- ✅ 持久 Bash session
- ✅ StreamingToolExecutor 并发调度

M1 的交付物符合路线图第 255 行的要求："工具集完整，并发可测"。

## 下一步

可以进入 **M2 治理层**（第 5-6 周）：
- 权限引擎四模式
- Hooks 五事件
- bashguard AST 分析 + 对抗测试表
- 审计 JSONL
- SQLite 存储 + `--resume`
