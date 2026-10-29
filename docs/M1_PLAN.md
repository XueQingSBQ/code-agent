# M1 里程碑实施计划 - 工具层完善

> 时间：第 3-4 周 | 状态：进行中

---

## 目标

完善工具层，使其达到生产级标准：
- 工具契约四道防线（已完成）
- 持久化 Bash 会话（**核心新增**）
- 更多文件操作（move/delete/list）
- 文件监视（可选，看时间）
- 并发执行器完善（已基本完成）

---

## 核心任务

### 1. 持久化 Bash 会话 ⭐⭐⭐

**问题**：
- M0 的 `ShellTool` 每次 `run()` 都是新进程
- 无法保留状态：`cd`、环境变量、shell 函数
- 多轮调试场景体验差

**设计**：
```python
class BashSession:
    """持久化的 bash 进程，支持多轮交互"""
    
    def __init__(self, workspace: Path, timeout: float = 120.0):
        self._workspace = workspace
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._output_buffer: asyncio.Queue[str] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
    
    async def start(self) -> None:
        """启动持久化 bash 进程"""
        # 启动 bash with PTY（保留颜色输出）
        # 设置 PS1 为特殊标记用于分隔命令输出
        # 启动后台读取任务
    
    async def execute(self, command: str) -> tuple[str, int]:
        """在会话中执行命令，返回 (output, exit_code)"""
        # 写入命令
        # 等待特殊标记
        # 解析退出码
    
    async def close(self) -> None:
        """优雅关闭会话"""
```

**关键细节**：
- 用特殊 `PS1` 标记分隔输出（比如 `__CARBIDE_PROMPT__`）
- 每个命令后 `echo $?` 获取退出码
- 超时保护：单个命令超时不杀整个会话，只中断当前命令
- 工作目录初始化为 workspace

**测试重点**：
- ✅ 多轮命令状态保留（`cd` + `pwd`）
- ✅ 环境变量传递（`export VAR=val` + `echo $VAR`）
- ✅ 超时不破坏会话（timeout 后仍可执行下一条）
- ✅ 退出码正确捕获
- ✅ 大输出不阻塞（异步读取）

**优先级**：🔥 高 - M1 核心交付物

---

### 2. 文件操作扩展 ⭐⭐

**新增工具**：

#### 2.1 ListTool (ls)
```python
class ListTool(Tool):
    """列出目录内容，支持递归和过滤"""
    concurrency_safe = True
    
    path: str  # 相对或绝对路径
    recursive: bool = False  # -R
    show_hidden: bool = False  # -a
```

**输出格式**：
```
src/
  __init__.py
  engine/
    __init__.py
    loop.py (459 lines)
    executor.py (187 lines)
```

#### 2.2 MoveTool (mv)
```python
class MoveTool(Tool):
    """移动/重命名文件或目录"""
    concurrency_safe = False  # 写操作
    
    source: str
    destination: str
```

#### 2.3 DeleteTool (rm)
```python
class DeleteTool(Tool):
    """删除文件或目录（需要权限确认）"""
    concurrency_safe = False  # 危险操作
    
    path: str
    recursive: bool = False  # 删除目录需要 True
```

**安全检查**：
- 所有路径都通过 `resolve_in_workspace()` 验证
- 删除操作必须经过权限引擎（M2 实现，M1 先硬编码拒绝递归删除）
- 不允许删除 `.git` 目录

**测试重点**：
- ✅ 工作区包含检查
- ✅ 删除不存在的文件报错清晰
- ✅ move 覆盖已存在文件的行为
- ✅ 递归删除安全拦截

**优先级**：🔥 高 - 基础工具集

---

### 3. 工具注册表增强 ⭐

**当前问题**：
- `ToolRegistry` 只是简单的 dict
- 没有工具分组
- 没有动态启用/禁用

**改进**：
```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
    
    def register(self, tool: Tool, *, enabled: bool = True) -> None:
        """注册工具，可选禁用"""
    
    def disable(self, name: str) -> None:
        """临时禁用工具（比如测试时）"""
    
    def enable(self, name: str) -> None:
        """重新启用工具"""
    
    def list_available(self) -> list[str]:
        """返回当前可用的工具名列表"""
```

**优先级**：⭐ 中 - 改善测试体验

---

### 4. 错误消息改进 ⭐⭐

**原则**：错误消息是**模型读取的可操作提示**

**改进清单**：
- ✅ M0 已完成：文件不存在 → 建议用 glob 搜索
- ✅ M0 已完成：路径逃逸 → 清晰说明工作区边界
- 🔲 Bash 命令失败 → 包含 stderr 和退出码
- 🔲 编辑失败（old_string 不匹配）→ 显示最相似的 5 行
- 🔲 权限拒绝 → 说明需要什么权限

**优先级**：⭐ 中 - 持续优化

---

## 测试策略

### 核心测试（必须完成）

1. **`tests/test_bash_session.py`** - 持久化 Bash 会话
   - `test_state_preserved_across_commands` - cd + 环境变量
   - `test_exit_code_captured` - 成功/失败退出码
   - `test_timeout_does_not_kill_session` - 超时恢复
   - `test_large_output_not_blocked` - 异步读取大输出
   - `test_concurrent_sessions_isolated` - 多会话隔离
   - **预计**：~8 个测试

2. **`tests/test_file_ops.py`** - 新文件操作工具
   - `test_list_directory` - 列出内容
   - `test_list_recursive` - 递归列出
   - `test_move_file` - 移动文件
   - `test_delete_file` - 删除文件
   - `test_delete_rejects_dotgit` - 安全拦截
   - `test_all_ops_respect_workspace` - 包含检查
   - **预计**：~10 个测试

3. **集成测试增强** - 更新 `test_integration.py`
   - `test_agent_uses_persistent_bash` - 多轮 bash 交互
   - `test_agent_moves_and_edits_file` - 文件操作组合
   - **预计**：+2 个测试

### 次要测试（可延后）

- 文件监视（如果实现）
- 工具注册表的 enable/disable
- 错误消息的具体文本

---

## 交付标准

### 功能完整性

- ✅ 持久化 Bash 会话工作
- ✅ 新文件操作工具（list/move/delete）可用
- ✅ 所有工具通过工作区包含检查
- ✅ 错误消息清晰可操作

### 质量门禁

- ✅ ruff check 通过
- ✅ mypy strict 通过
- ✅ import-linter 契约保持
- ✅ 测试覆盖率 ≥ 82%（允许从 85% 略降，因为新代码）
- ✅ 所有核心测试通过

### 文档

- ✅ 更新 README 的工具列表
- ✅ 添加 Bash 会话使用示例
- ✅ 更新面试 Q&A（如果有新的设计亮点）

---

## 时间分配（业余 2 周）

### Week 1（第 3 周）
- **Day 1-2**：实现 `BashSession` 类
- **Day 3-4**：实现新文件操作工具
- **Day 5-7**：核心测试编写

### Week 2（第 4 周）
- **Day 1-2**：集成测试和修 bug
- **Day 3-4**：质量门禁修复（类型错误、lint）
- **Day 5-6**：文档更新
- **Day 7**：验收和 git commit

---

## 已完成的 M0 基础

- ✅ 工具契约四道防线
- ✅ StreamingToolExecutor 并发执行
- ✅ 工作区包含检查（`tools/paths.py`）
- ✅ 基础工具（read/write/edit/grep/glob/shell）
- ✅ 85% 测试覆盖率，152 个测试

---

## 下一步（M2 预览）

M1 完成后，M2 会在工具层之上添加：
- 权限引擎（4 种模式）
- Hooks（5 个事件点）
- bashguard（tree-sitter AST 分析）
- 审计日志（append-only JSONL）
- SQLite 存储 + `--resume`

M1 的工具层设计需要为 M2 预留扩展点，但不需要现在实现。
