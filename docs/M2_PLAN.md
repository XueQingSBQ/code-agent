# M2 里程碑实施计划 - 治理层

> 时间：第 5-6 周 | 状态：进行中

---

## 目标

构建安全可控的治理层，使 Agent 在生产环境中可信：
- 权限引擎（4 种模式）
- Hooks 系统（5 个事件点）
- bashguard（AST 分析防护）
- 审计日志（JSONL）
- SQLite 会话存储 + `--resume`

---

## 核心任务

### 1. 权限引擎 ⭐⭐⭐

**问题**：
- M0/M1 工具直接执行，没有拦截点
- 无法控制 Agent 的破坏性操作（删除文件、执行 shell）
- 生产环境需要人工审批或策略控制

**设计**：

#### 1.1 权限模式（4 种）

```python
class PermissionMode(Enum):
    """权限策略"""
    ALWAYS_ALLOW = "always_allow"  # 自动化场景，完全信任
    PROMPT = "prompt"              # 人工审批（默认）
    AUDIT = "audit"                # 记录但不拦截
    DENY = "deny"                  # 拒绝所有危险操作
```

#### 1.2 工具风险级别

```python
class RiskLevel(Enum):
    SAFE = "safe"           # Read, Grep, Glob
    WRITE = "write"         # Write, Edit
    DESTRUCTIVE = "destructive"  # Delete, Bash
```

每个工具声明自己的风险级别：
```python
class DeleteTool(Tool):
    risk_level = RiskLevel.DESTRUCTIVE
    
    async def run(self, args, ctx):
        # 实现会在权限检查通过后才执行
        ...
```

#### 1.3 权限引擎核心

```python
class PermissionEngine:
    """权限决策引擎"""
    
    def __init__(self, mode: PermissionMode):
        self._mode = mode
        self._audit_log: list[AuditEntry] = []
    
    async def check(
        self, 
        tool: Tool, 
        args: BaseModel,
        ctx: ToolContext
    ) -> PermissionDecision:
        """
        返回 PermissionDecision:
          - approved: bool
          - reason: str | None  # 拒绝原因
          - should_audit: bool
        """
        risk = tool.risk_level
        
        # ALWAYS_ALLOW: 全部通过
        if self._mode == PermissionMode.ALWAYS_ALLOW:
            return PermissionDecision(approved=True, should_audit=False)
        
        # DENY: 拒绝危险操作
        if self._mode == PermissionMode.DENY:
            if risk == RiskLevel.DESTRUCTIVE:
                return PermissionDecision(
                    approved=False,
                    reason=f"{tool.name} is destructive and denied by policy"
                )
        
        # AUDIT: 记录所有操作但不拦截
        if self._mode == PermissionMode.AUDIT:
            return PermissionDecision(approved=True, should_audit=True)
        
        # PROMPT: 危险操作需要人工确认
        if self._mode == PermissionMode.PROMPT:
            if risk == RiskLevel.DESTRUCTIVE:
                approved = await self._prompt_user(tool, args)
                return PermissionDecision(
                    approved=approved,
                    should_audit=True
                )
        
        return PermissionDecision(approved=True, should_audit=False)
    
    async def _prompt_user(self, tool: Tool, args: BaseModel) -> bool:
        """显示确认对话框，返回用户决策"""
        # M2 先用简单的 input()，M3 集成 Rich TUI
        print(f"\n⚠️  {tool.name} is requesting to execute:")
        print(f"   {args}")
        response = input("Approve? [y/N]: ")
        return response.lower() == 'y'
```

#### 1.4 集成到工具执行

修改 `StreamingToolExecutor._run()`：

```python
async def _run(
    self, call: StreamToolUse, waits: list[asyncio.Task[CompletedTool]]
) -> CompletedTool:
    if waits:
        await asyncio.wait(waits)
    
    started = time.perf_counter()
    
    # 新增：权限检查
    tool = self._registry.get_tool(call.name)
    decision = await self._permission_engine.check(tool, call.arguments, self._ctx)
    
    if not decision.approved:
        # 拒绝时返回错误结果，不抛异常
        return CompletedTool(
            call=call,
            result=ToolResult(
                content=f"Permission denied: {decision.reason}",
                is_error=True
            ),
            duration_ms=0.0
        )
    
    # 审计记录
    if decision.should_audit:
        self._permission_engine.audit(call, "approved")
    
    # 原有的工具执行逻辑
    result = await self._registry.invoke(call.name, call.arguments, self._ctx)
    
    if decision.should_audit:
        self._permission_engine.audit(call, "completed", result)
    
    return CompletedTool(
        call=call, result=result, duration_ms=(time.perf_counter() - started) * 1000
    )
```

**测试重点**：
- `test_always_allow_mode` - 所有工具通过
- `test_deny_mode_blocks_destructive` - 删除/bash 被拒绝
- `test_prompt_mode_accepts_user_input` - 模拟用户批准/拒绝
- `test_audit_mode_logs_all_operations` - 审计记录完整
- `test_denied_tool_returns_error_result` - 拒绝时不抛异常

**优先级**：🔥 高 - M2 核心交付物

---

### 2. Hooks 系统 ⭐⭐⭐

**问题**：
- 用户无法注入自定义逻辑（比如通知、日志）
- 测试时无法 mock 工具行为
- 无法实现工具调用的前置/后置处理

**设计**：

#### 2.1 Hook 事件点（5 个）

```python
class HookEvent(Enum):
    PRE_TOOL = "pre_tool"       # 工具执行前
    POST_TOOL = "post_tool"     # 工具执行后
    ON_ERROR = "on_error"       # 工具抛异常时
    PRE_API_CALL = "pre_api_call"   # 调用 API 前
    POST_API_CALL = "post_api_call" # API 返回后
```

#### 2.2 Hook 接口

```python
class Hook(ABC):
    """Hook 基类"""
    
    @abstractmethod
    async def on_pre_tool(
        self, 
        tool_name: str, 
        args: BaseModel,
        ctx: ToolContext
    ) -> None:
        """工具执行前调用，可抛异常来阻止执行"""
        pass
    
    async def on_post_tool(
        self,
        tool_name: str,
        args: BaseModel,
        result: ToolResult,
        duration_ms: float
    ) -> None:
        """工具执行后调用"""
        pass
    
    async def on_error(
        self,
        tool_name: str,
        args: BaseModel,
        error: Exception
    ) -> None:
        """工具执行出错时调用"""
        pass
```

#### 2.3 HookManager

```python
class HookManager:
    """管理和调度 hooks"""
    
    def __init__(self):
        self._hooks: list[Hook] = []
    
    def register(self, hook: Hook) -> None:
        self._hooks.append(hook)
    
    async def trigger_pre_tool(
        self, tool_name: str, args: BaseModel, ctx: ToolContext
    ) -> None:
        for hook in self._hooks:
            await hook.on_pre_tool(tool_name, args, ctx)
    
    async def trigger_post_tool(
        self,
        tool_name: str,
        args: BaseModel,
        result: ToolResult,
        duration_ms: float
    ) -> None:
        for hook in self._hooks:
            await hook.on_post_tool(tool_name, args, result, duration_ms)
    
    async def trigger_error(
        self, tool_name: str, args: BaseModel, error: Exception
    ) -> None:
        for hook in self._hooks:
            await hook.on_error(tool_name, args, error)
```

#### 2.4 内置 Hooks

**LoggingHook** - 结构化日志：
```python
class LoggingHook(Hook):
    async def on_pre_tool(self, tool_name, args, ctx):
        log.info("tool_started", tool=tool_name, args=args.dict())
    
    async def on_post_tool(self, tool_name, args, result, duration_ms):
        log.info("tool_completed", tool=tool_name, 
                 duration_ms=duration_ms, is_error=result.is_error)
```

**TimingHook** - 性能监控：
```python
class TimingHook(Hook):
    def __init__(self):
        self.timings: dict[str, list[float]] = defaultdict(list)
    
    async def on_post_tool(self, tool_name, args, result, duration_ms):
        self.timings[tool_name].append(duration_ms)
    
    def report(self) -> str:
        lines = ["Tool Performance:"]
        for tool, times in self.timings.items():
            avg = sum(times) / len(times)
            lines.append(f"  {tool}: {avg:.2f}ms avg ({len(times)} calls)")
        return "\n".join(lines)
```

**测试重点**：
- `test_hook_execution_order` - 多个 hooks 按注册顺序执行
- `test_pre_tool_hook_can_block_execution` - pre_tool 抛异常阻止工具
- `test_error_hook_triggered_on_exception` - 异常时触发 on_error
- `test_hooks_isolated_from_each_other` - 一个 hook 失败不影响其他

**优先级**：🔥 高 - M2 核心交付物

---

### 3. bashguard（AST 分析）⭐⭐

**问题**：
- Bash 命令可能包含危险操作：`rm -rf /`、`:(){ :|:& };:`（fork bomb）
- 简单字符串检查容易绕过：`r''m -rf /`
- 需要真正的语法树分析

**设计**：

#### 3.1 使用 tree-sitter-bash

```python
from tree_sitter import Language, Parser
import tree_sitter_bash

# 初始化
BASH_LANGUAGE = Language(tree_sitter_bash.language())
parser = Parser(BASH_LANGUAGE)
```

#### 3.2 危险模式检测

```python
class BashGuard:
    """Bash 命令安全分析器"""
    
    DANGEROUS_COMMANDS = {
        "rm", "dd", "mkfs", "fdisk", "parted",
        "chmod", "chown",  # 批量权限修改
    }
    
    DANGEROUS_PATHS = {
        "/", "/bin", "/etc", "/usr", "/var", "/sys", "/proc"
    }
    
    def analyze(self, command: str) -> GuardResult:
        """
        返回 GuardResult:
          - safe: bool
          - violations: list[Violation]
        """
        tree = parser.parse(bytes(command, "utf8"))
        violations = []
        
        # 遍历 AST 节点
        self._visit(tree.root_node, violations)
        
        return GuardResult(
            safe=len(violations) == 0,
            violations=violations
        )
    
    def _visit(self, node, violations):
        # 检测 rm 命令
        if node.type == "command" and node.children:
            cmd_name = node.children[0].text.decode()
            if cmd_name in self.DANGEROUS_COMMANDS:
                # 检查参数
                for child in node.children[1:]:
                    if child.type == "concatenation":
                        # rm -rf 或 rm -fr
                        if b"-rf" in child.text or b"-fr" in child.text:
                            violations.append(Violation(
                                type="destructive_recursive_delete",
                                node=node,
                                message="Detected 'rm -rf'"
                            ))
                    
                    # 检查路径
                    if child.type == "word":
                        path = child.text.decode()
                        if path in self.DANGEROUS_PATHS:
                            violations.append(Violation(
                                type="system_path_deletion",
                                node=node,
                                message=f"Attempting to delete system path: {path}"
                            ))
        
        # 递归检查子节点
        for child in node.children:
            self._visit(child, violations)
```

#### 3.3 集成到 BashTool

```python
class BashTool(Tool):
    def __init__(self):
        self._guard = BashGuard()
    
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        # 安全检查
        guard_result = self._guard.analyze(args.command)
        if not guard_result.safe:
            violations_text = "\n".join(
                f"- {v.message}" for v in guard_result.violations
            )
            return ToolResult(
                content=f"Command blocked by bashguard:\n{violations_text}",
                is_error=True
            )
        
        # 原有执行逻辑
        ...
```

**测试重点**：
- `test_blocks_rm_rf_slash` - 拦截 `rm -rf /`
- `test_blocks_dd_overwrite` - 拦截 `dd if=/dev/zero of=/dev/sda`
- `test_allows_safe_rm` - 允许 `rm temp.txt`
- `test_detects_obfuscated_commands` - 检测 `r''m -rf`
- `test_fork_bomb_detection` - 拦截 `:(){ :|:& };:`

**优先级**：⭐⭐ 高 - 安全关键

---

### 4. 审计日志（JSONL）⭐⭐

**问题**：
- 没有持久化的操作记录
- 无法追踪 Agent 做了什么
- 生产环境需要合规审计

**设计**：

#### 4.1 审计日志格式

```python
@dataclass
class AuditEntry:
    timestamp: str  # ISO 8601
    session_id: str
    event_type: str  # "tool_call", "tool_result", "api_request", "permission_check"
    tool_name: str | None
    args: dict[str, Any] | None
    result: str | None
    is_error: bool
    duration_ms: float | None
    permission_decision: str | None  # "approved" / "denied" / null
```

每条记录一行 JSON（JSONL 格式）：
```json
{"timestamp":"2024-09-16T16:00:00Z","session_id":"abc123","event_type":"tool_call","tool_name":"bash","args":{"command":"ls -la"},"result":null,"is_error":false,"duration_ms":null,"permission_decision":"approved"}
{"timestamp":"2024-09-16T16:00:01Z","session_id":"abc123","event_type":"tool_result","tool_name":"bash","args":null,"result":"total 48\ndrwxr-xr-x...","is_error":false,"duration_ms":142.5,"permission_decision":null}
```

#### 4.2 AuditLogger

```python
class AuditLogger:
    """Append-only 审计日志"""
    
    def __init__(self, log_path: Path, session_id: str):
        self._log_path = log_path
        self._session_id = session_id
        self._log_file: TextIO | None = None
    
    def open(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self._log_path, "a", encoding="utf-8")
    
    def log(self, entry: AuditEntry) -> None:
        if self._log_file is None:
            raise RuntimeError("AuditLogger not opened")
        line = json.dumps(asdict(entry), ensure_ascii=False)
        self._log_file.write(line + "\n")
        self._log_file.flush()  # 立即刷盘
    
    def close(self) -> None:
        if self._log_file:
            self._log_file.close()
```

#### 4.3 集成到 PermissionEngine

```python
class PermissionEngine:
    def __init__(self, mode: PermissionMode, audit_logger: AuditLogger):
        self._mode = mode
        self._audit_logger = audit_logger
    
    async def check(self, tool, args, ctx) -> PermissionDecision:
        decision = ... # 原有逻辑
        
        # 记录审计
        self._audit_logger.log(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            session_id=ctx.session_id,
            event_type="permission_check",
            tool_name=tool.name,
            args=args.dict(),
            result=None,
            is_error=False,
            duration_ms=None,
            permission_decision="approved" if decision.approved else "denied"
        ))
        
        return decision
```

**测试重点**：
- `test_audit_log_is_append_only` - 不覆盖已有记录
- `test_audit_log_survives_crash` - flush 后立即可读
- `test_audit_entries_are_valid_json` - 每行可独立解析
- `test_audit_includes_all_tool_calls` - 无遗漏

**优先级**：⭐⭐ 高 - 合规要求

---

### 5. SQLite 会话存储 + `--resume` ⭐

**问题**：
- 会话状态只在内存，进程退出后丢失
- 无法从中断点恢复长时间运行的任务
- 用户想查看历史会话

**设计**：

#### 5.1 Schema

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL  -- "running", "completed", "failed"
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,  -- "user", "assistant"
    content TEXT NOT NULL,  -- JSON serialized
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_messages_session ON messages(session_id);
```

#### 5.2 SessionStore

```python
class SessionStore:
    """SQLite 会话存储"""
    
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
    
    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")  # 性能优化
        self._init_schema()
    
    def _init_schema(self) -> None:
        # 创建表（如果不存在）
        self._conn.executescript(SCHEMA_SQL)
    
    def create_session(self, session_id: str, workspace: Path) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, workspace, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, now, now, str(workspace), "running")
        )
        self._conn.commit()
    
    def append_message(self, session_id: str, role: str, content: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, json.dumps(content), now)
        )
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id)
        )
        self._conn.commit()
    
    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,)
        )
        messages = []
        for row in cursor:
            role, content_json = row
            messages.append({
                "role": role,
                "content": json.loads(content_json)
            })
        return messages
```

#### 5.3 CLI 集成

```python
# app.py
@click.command()
@click.option("--resume", type=str, help="Resume session ID")
def main(resume: str | None):
    store = SessionStore(Path.home() / ".carbide" / "sessions.db")
    store.open()
    
    if resume:
        # 恢复会话
        messages = store.load_messages(resume)
        session_id = resume
        print(f"Resuming session {session_id} with {len(messages)} messages")
    else:
        # 新会话
        session_id = uuid.uuid4().hex[:8]
        store.create_session(session_id, workspace)
        messages = []
    
    # 启动循环
    ...
```

**测试重点**：
- `test_create_and_load_session` - 存储后能恢复
- `test_resume_preserves_message_order` - 消息顺序不变
- `test_concurrent_sessions_isolated` - 多会话不冲突
- `test_invalid_session_id_raises` - 恢复不存在的会话报错

**优先级**：⭐ 中 - 改善用户体验

---

## 测试策略

### 核心测试（必须完成）

1. **`tests/test_permission_engine.py`** - 权限引擎
   - 4 种模式的完整覆盖
   - 拒绝时返回错误结果而非异常
   - **预计**：~12 个测试

2. **`tests/test_hooks.py`** - Hooks 系统
   - Hook 执行顺序
   - pre_tool 阻止执行
   - 错误隔离
   - **预计**：~8 个测试

3. **`tests/test_bashguard.py`** - AST 分析
   - 危险命令检测
   - 路径验证
   - 混淆命令识别
   - **预计**：~10 个测试

4. **`tests/test_audit_logger.py`** - 审计日志
   - Append-only 保证
   - JSONL 格式正确
   - 崩溃恢复
   - **预计**：~6 个测试

5. **`tests/test_session_store.py`** - SQLite 存储
   - CRUD 操作
   - `--resume` 恢复
   - **预计**：~8 个测试

### 集成测试

- `test_permission_engine_integrates_with_executor` - 权限引擎 + 执行器
- `test_hooks_trigger_during_tool_execution` - Hooks 在实际执行中触发
- `test_full_audit_trail` - 完整审计链

---

## 交付标准

### 功能完整性

- ✅ 权限引擎 4 种模式工作
- ✅ Hooks 正确触发
- ✅ bashguard 拦截危险命令
- ✅ 审计日志持久化
- ✅ `--resume` 恢复会话

### 质量门禁

- ✅ ruff check 通过
- ✅ mypy strict 通过
- ✅ 测试覆盖率 ≥ 80%
- ✅ 所有核心测试通过

### 文档

- ✅ 更新 README 添加权限说明
- ✅ 添加 bashguard 示例
- ✅ 更新面试 Q&A（M2 设计亮点）

---

## 时间分配（业余 2 周）

### Week 1（第 5 周）
- **Day 1-2**：权限引擎实现
- **Day 3-4**：Hooks 系统
- **Day 5-7**：bashguard AST 分析

### Week 2（第 6 周）
- **Day 1-2**：审计日志 + SQLite 存储
- **Day 3-4**：集成测试和 bug 修复
- **Day 5-6**：文档更新
- **Day 7**：验收

---

## 架构预留

M2 的治理层需要为 M3 预留扩展点：
- 权限决策支持上下文大小限制（M3 的压缩触发条件）
- Hooks 支持异步事件（M3 的 background snip）
- 审计日志支持查询（M4 的记忆检索）

但这些扩展点现在不需要实现，只需要接口设计时考虑可扩展性。
