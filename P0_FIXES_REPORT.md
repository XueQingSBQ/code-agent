# P0 问题修复报告

## 已修复问题

### ✅ P0-1: pyproject.toml 配置错误
**问题**: dependencies 列表位于 `[project.urls]` section 下，导致构建失败
**修复**: 
- 将 `dependencies` 移到 `[project.urls]` 之前
- 添加 `[tool.hatch.build.targets.sdist]` 排除 eval_results 等文件
**验证**: `uv build` 成功生成 wheel 和 sdist

---

### ✅ P0-2: Governance 运行时失败

#### 问题 1: AuditLogger 未正确打开
**问题**: AuditLogger 创建后未调用 `.open()`，且传入目录而非文件路径
**修复**: 
```python
audit_log_path = audit_dir / f"{session_id}.jsonl"
self.audit_logger = AuditLogger(log_path=audit_log_path, session_id=session_id)
self.audit_logger.open()
```

#### 问题 2: 方法参数不一致
**问题**: Executor 调用 `log_permission_check(arguments=..., result=...)`
**实际**: AuditLogger 签名为 `log_permission_check(args=..., decision=...)`
**修复**: 统一参数名为 `args` 和 `decision`

#### 问题 3: 缺失方法 log_tool_error
**问题**: Executor 调用不存在的 `log_tool_error()`
**实际**: AuditLogger 只有 `log_error()`
**修复**: 改用 `log_error(tool_name, error_msg)`

#### 问题 4: BashGuard API 不匹配
**问题**: 
- Executor 期望 `analysis.safe` 和 `analysis.issues`
- BashGuard.analyze() 返回 `list[Finding]`
**修复**: 
```python
is_safe = self._bash_guard.is_safe(command)
if not is_safe:
    findings = self._bash_guard.analyze(command)
    issues = [f.message for f in findings]
```

#### 问题 5: Hooks API 不匹配
**问题**: Executor 调用不存在的 `run_before_async()` 和 `run_after_async()`
**实际**: HookRegistry 只有同步方法 `run_before()` 和 `run_after()`
**修复**: 改用同步调用并传入正确的 HookContext

#### 问题 6: log_tool_execution 不存在
**问题**: Executor 调用不存在的 `log_tool_execution()`
**实际**: AuditLogger 有 `log_tool_call()` 和 `log_tool_result()`
**修复**: 改用 `log_tool_result()`

---

### ✅ P0-3: 资源生命周期问题
**问题**: `Agent.__aexit__` 为空，未关闭 AuditLogger 和 SessionStore
**修复**: 
```python
async def __aexit__(self, exc_type, exc_val, exc_tb):
    # Close audit logger
    if self.audit_logger is not None:
        self.audit_logger.close()
    
    # Close session store
    if self.session_store is not None:
        self.session_store.close()
    
    # Close provider if it has cleanup
    if hasattr(self.provider, 'close'):
        await self.provider.close()
```

---

## 修改的文件

1. `pyproject.toml` - 修复构建配置
2. `src/carbide/sdk.py` - 修复 AuditLogger 初始化和资源清理
3. `src/carbide/engine/executor.py` - 修复所有 Governance 组件调用

---

## 下一步

需要验证修复：
1. 创建集成测试验证 Governance 运行时
2. 测试带治理的工具执行
3. 验证资源正确关闭

---

**修复日期**: 2026-09-17
**状态**: 所有 P0 问题已修复，待验证
