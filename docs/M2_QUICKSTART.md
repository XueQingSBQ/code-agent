# M2 Governance Layer - Quick Start

5 分钟上手 M2 治理系统。

## 基础使用

### 1. 权限引擎

控制哪些工具可以执行：

```python
from carbide.governance import PermissionEngine, PermissionMode

# 创建权限引擎
engine = PermissionEngine(mode=PermissionMode.AUTO)

# 检查工具执行权限
from carbide.tools.fs import WriteTool

tool = WriteTool()
args = WriteParams(path="test.txt", content="hello")

result = engine.check(tool, args)

if result.allowed:
    print("执行允许")
else:
    print(f"需要用户确认: {result.prompt}")
```

**三种模式**：

```python
# AUTO - 安全操作自动通过，危险操作需要确认（推荐）
PermissionMode.AUTO

# ALWAYS - 允许所有操作（自动化场景）
PermissionMode.ALWAYS

# NEVER - 阻止所有写入/破坏性操作（只读模式）
PermissionMode.NEVER
```

### 2. Hook 系统

在工具执行前后插入自定义逻辑：

```python
from carbide.governance import HookRegistry

registry = HookRegistry()

# Before hook - 在执行前拦截
def audit_log(tool, ctx):
    print(f"📝 执行工具: {ctx.tool_name}")
    print(f"   参数: {ctx.arguments}")
    print(f"   工作空间: {ctx.workspace}")
    return None  # 返回 None 表示允许继续执行

registry.register_before(audit_log)

# After hook - 在执行后处理结果
def result_logger(tool, ctx, result):
    if result.is_error:
        print(f"❌ {ctx.tool_name} 失败: {result.content}")
    else:
        print(f"✅ {ctx.tool_name} 成功")
    return result  # 必须返回 result

registry.register_after(result_logger)
```

**短路执行**：

```python
def block_dangerous(tool, ctx):
    if "prod" in ctx.workspace:
        return ToolResult(
            content="阻止在生产环境执行",
            is_error=True
        )
    return None  # 允许执行

registry.register_before(block_dangerous)
```

### 3. BashGuard 命令分析

检测危险的 shell 命令：

```python
from carbide.governance import BashGuard, Severity

guard = BashGuard()

# 分析命令
findings = guard.analyze("rm -rf /tmp/*")

for finding in findings:
    if finding.severity == Severity.CRITICAL:
        print(f"🚨 严重: {finding.message}")
    elif finding.severity == Severity.WARNING:
        print(f"⚠️  警告: {finding.message}")
```

**检测模式**：

```python
# 递归删除
guard.analyze("rm -rf folder")  # WARNING: Recursive delete

# 危险路径
guard.analyze("rm -rf /")       # CRITICAL: Dangerous absolute path

# 输出重定向
guard.analyze("echo '' > file") # WARNING: Destructive redirect

# 嵌套替换
guard.analyze("$(cat $(find .))")  # WARNING: Nested command substitution
```

## 集成示例

将三个组件组合使用：

```python
from carbide.governance import (
    PermissionEngine,
    PermissionMode,
    HookRegistry,
    BashGuard,
    Severity,
)
from carbide.tools.shell import BashTool, BashParams
from carbide.tools.base import ToolContext, ToolResult

# 1. 设置组件
engine = PermissionEngine(mode=PermissionMode.AUTO)
hooks = HookRegistry()
guard = BashGuard()

# 2. 注册 BashGuard 作为 before hook
def safety_check(tool, ctx):
    if tool.name == "bash":
        findings = guard.analyze(ctx.arguments.command)
        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        if critical:
            return ToolResult(
                content=f"🚨 命令被阻止: {critical[0].message}",
                is_error=True,
            )
    return None

hooks.register_before(safety_check)

# 3. 注册审计日志
def audit(tool, ctx):
    print(f"📝 [{ctx.tool_name}] {ctx.arguments}")
    return None

hooks.register_before(audit)

# 4. 执行工具（带完整保护）
async def execute_with_governance(tool, args, ctx):
    # 权限检查
    perm_result = engine.check(tool, args)
    if not perm_result.allowed:
        print(perm_result.prompt)
        # 在真实场景中，这里需要等待用户确认
        return ToolResult(content="Permission denied", is_error=True)
    
    # 运行 before hooks
    hook_result = hooks.run_before(
        tool,
        HookContext(
            tool_name=tool.name,
            arguments=args,
            workspace=str(ctx.workspace),
        ),
    )
    if hook_result:
        return hook_result  # 被 hook 短路
    
    # 执行工具
    result = await tool.run(args, ctx)
    
    # 运行 after hooks
    final_result = hooks.run_after(
        tool,
        HookContext(
            tool_name=tool.name,
            arguments=args,
            workspace=str(ctx.workspace),
        ),
        result,
    )
    
    return final_result

# 使用示例
import asyncio
from pathlib import Path

async def main():
    tool = BashTool()
    args = BashParams(command="echo 'Hello, M2!'")
    ctx = ToolContext(workspace=Path("."), shell="bash")
    
    result = await execute_with_governance(tool, args, ctx)
    print(f"\n结果: {result.content}")

asyncio.run(main())
```

## 常见使用场景

### 场景 1：只读模式

```python
# 只允许读取操作，阻止所有修改
engine = PermissionEngine(mode=PermissionMode.NEVER)
```

### 场景 2：审计所有工具调用

```python
hooks = HookRegistry()

def audit_trail(tool, ctx):
    with open("audit.log", "a") as f:
        f.write(f"{datetime.now()} | {ctx.tool_name} | {ctx.arguments}\n")
    return None

hooks.register_before(audit_trail)
```

### 场景 3：命令白名单

```python
ALLOWED_COMMANDS = {"ls", "pwd", "echo", "cat"}

def whitelist_check(tool, ctx):
    if tool.name == "bash":
        cmd = ctx.arguments.command.split()[0]
        if cmd not in ALLOWED_COMMANDS:
            return ToolResult(
                content=f"命令 '{cmd}' 不在白名单中",
                is_error=True,
            )
    return None

hooks.register_before(whitelist_check)
```

### 场景 4：自动备份

```python
import shutil
from datetime import datetime

def auto_backup(tool, ctx):
    if tool.name in ["write", "edit"]:
        path = ctx.arguments.path
        if Path(path).exists():
            backup = f"{path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(path, backup)
    return None

hooks.register_before(auto_backup)
```

### 场景 5：结果过滤

```python
def sanitize_output(tool, ctx, result):
    # 从输出中移除敏感信息
    content = result.content
    content = content.replace("secret_key=abc123", "secret_key=***")
    content = content.replace("password: admin", "password: ***")
    
    return ToolResult(
        content=content,
        is_error=result.is_error,
    )

hooks.register_after(sanitize_output)
```

## 风险等级

所有工具都有 `risk_level` 属性：

```python
from carbide.governance import RiskLevel

# SAFE - 只读操作
# - glob, grep, read
RiskLevel.SAFE

# WRITE - 文件修改
# - write, edit
RiskLevel.WRITE

# DESTRUCTIVE - 危险操作
# - bash, persistent_bash
RiskLevel.DESTRUCTIVE
```

检查工具的风险等级：

```python
from carbide.tools.fs import WriteTool
from carbide.tools.shell import BashTool

print(WriteTool.risk_level)      # RiskLevel.WRITE
print(BashTool.risk_level)        # RiskLevel.DESTRUCTIVE
```

## 运行示例

```bash
# 完整集成示例
python examples/m2_integration.py

# 运行测试
python -m pytest tests/test_permission.py -v   # 权限引擎测试
python -m pytest tests/test_hooks.py -v        # Hook 系统测试
python -m pytest tests/test_bashguard.py -v    # BashGuard 测试

# 所有 M2 测试
python -m pytest tests/test_permission.py tests/test_hooks.py tests/test_bashguard.py -v
# 预期: 27 passed
```

## 下一步

查看完整文档：
- [M2_COMPLETION.md](M2_COMPLETION.md) - 完整特性和架构说明
- [../examples/m2_integration.py](../examples/m2_integration.py) - 完整工作示例
- API 文档在各模块的 docstring 中

---

**开始使用 M2 治理层，让你的工具执行更安全！**
