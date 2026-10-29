# M2 Governance Layer - Complete ✓

**Completion Date**: 2026-09-16  
**Status**: All 27 tests passing  
**Built on**: M1 (Tool layer + Persistent Bash, 87 tests)

## Overview

M2 adds a complete governance layer on top of M1's tool foundation:
- Permission engine with risk-based gating
- Hook system for tool execution interception  
- BashGuard AST analyzer for shell command safety

## Components Delivered

### 1. Permission Engine (`src/carbide/governance/permission.py`)

Risk-based tool execution gating with three modes:

```python
from carbide.governance import PermissionEngine, PermissionMode, RiskLevel

# AUTO mode: allow SAFE, prompt for WRITE/DESTRUCTIVE
engine = PermissionEngine(mode=PermissionMode.AUTO)

# Check before execution
result = engine.check(tool, args)
if not result.allowed:
    if result.prompt:
        # Show prompt to user
        print(result.prompt)
```

**Modes**:
- `AUTO`: Default. Safe operations allowed, risky ones require prompts
- `ALWAYS`: Allow everything (for trusted automation)
- `NEVER`: Block all write/destructive operations

**Risk Levels**:
- `SAFE`: Read-only operations (glob, grep, read)
- `WRITE`: File modifications (write, edit)
- `DESTRUCTIVE`: Irreversible operations (bash, rm, truncate)

### 2. Hook System (`src/carbide/governance/hooks.py`)

Intercept tool calls before and after execution:

```python
from carbide.governance import HookRegistry

registry = HookRegistry()

# Before hook: can short-circuit execution
def audit_hook(tool, ctx):
    print(f"Executing {ctx.tool_name} with {ctx.arguments}")
    return None  # Allow execution

registry.register_before(audit_hook)

# After hook: can modify results
def logging_hook(tool, ctx, result):
    print(f"Completed {ctx.tool_name}: {result.content[:50]}")
    return result

registry.register_after(logging_hook)
```

**Use Cases**:
- Audit trails and logging
- Custom permission checks
- Command rewriting for safety
- Metrics and telemetry

### 3. BashGuard AST Analyzer (`src/carbide/governance/bashguard.py`)

Static analysis of shell commands to detect dangerous patterns:

```python
from carbide.governance import BashGuard, Severity

guard = BashGuard()
findings = guard.analyze("rm -rf /")

for finding in findings:
    print(f"{finding.severity}: {finding.message}")
    # CRITICAL: Recursive delete with absolute path
```

**Detection Patterns**:
- Recursive deletes (`rm -rf`, `rm -r`)
- Destructive overwrites (`>`, truncate)
- Dangerous paths (`/`, `/usr`, `/etc`)
- Nested command substitutions
- Piped destructive commands
- Wildcard risks

**Severity Levels**:
- `INFO`: Minor issues, suggestions
- `WARNING`: Potentially risky operations
- `CRITICAL`: Dangerous operations that should be blocked

## Test Coverage

```bash
# M2 governance tests (27 tests)
pytest tests/test_permission.py    # 9 tests - Permission engine
pytest tests/test_hooks.py         # 6 tests - Hook system
pytest tests/test_bashguard.py     # 12 tests - AST analyzer

# Full M1 + M2 suite
pytest tests/  # 114 total tests passing
```

### Test Breakdown

**Permission Engine** (`test_permission.py`):
- AUTO mode allows safe operations
- AUTO mode blocks write operations with prompts
- AUTO mode blocks destructive operations with prompts
- ALWAYS mode allows everything
- NEVER mode blocks risky operations
- Mode can be changed at runtime
- PermissionResult has useful repr

**Hook System** (`test_hooks.py`):
- Before hooks can observe execution
- Before hooks can short-circuit execution
- After hooks can modify results
- Multiple hooks chain correctly
- Hooks can be cleared

**BashGuard** (`test_bashguard.py`):
- Detects recursive deletes
- Detects absolute path risks
- Detects dangerous directories
- Detects output redirects
- Detects nested substitutions
- Detects piped rm commands
- Allows safe commands
- Provides severity levels
- Handles complex real-world commands

## Integration Example

Complete governance stack in action:

```python
from carbide.governance import (
    PermissionEngine,
    PermissionMode,
    HookRegistry,
    BashGuard,
)
from carbide.tools.shell import BashTool

# Setup governance
engine = PermissionEngine(mode=PermissionMode.AUTO)
hooks = HookRegistry()
guard = BashGuard()

# Add bashguard as a before hook
def safety_check(tool, ctx):
    if tool.name == "bash":
        findings = guard.analyze(ctx.arguments.command)
        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        if critical:
            return ToolResult(
                content=f"Blocked: {critical[0].message}",
                is_error=True
            )
    return None

hooks.register_before(safety_check)

# Execute with full protection
tool = BashTool()
args = BashParams(command="rm -rf /tmp/test")

# 1. Check permissions
perm_result = engine.check(tool, args)
if not perm_result.allowed:
    print(perm_result.prompt)
    # Wait for user approval
    
# 2. Run hooks
hook_result = hooks.run_before(tool, HookContext(...))
if hook_result:
    print(hook_result.content)  # Blocked by safety check
else:
    # 3. Execute tool
    result = await tool.run(args, ctx)
    
    # 4. Post-process
    final_result = hooks.run_after(tool, HookContext(...), result)
```

## Architecture Notes

### Circular Import Resolution

The governance layer imports from `tools.base` (for `Tool` class), and `tools.base` imports from `governance.permission` (for `RiskLevel`). This creates a circular dependency.

**Solution**: Use `TYPE_CHECKING` guard in `hooks.py`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from carbide.tools.base import Tool, ToolResult
```

This allows type hints to work while avoiding runtime circular imports.

### Tool Classification

All tools now carry a `risk_level` ClassVar:

```python
class ReadTool(Tool):
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    
class WriteTool(Tool):
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    
class BashTool(Tool):
    risk_level: ClassVar[RiskLevel] = RiskLevel.DESTRUCTIVE
```

This enables the permission engine to gate execution based on tool classification.

## Public API

Exported from `carbide.governance`:

```python
from carbide.governance import (
    # Permission system
    PermissionEngine,
    PermissionMode,
    PermissionResult,
    RiskLevel,
    
    # Hook system
    HookRegistry,
    HookContext,
    BeforeHook,
    AfterHook,
    
    # BashGuard
    BashGuard,
    Finding,
    Severity,
)
```

## Next Steps: M3

M3 will add the executor that orchestrates tools with governance:
- Tool execution pipeline with permission checks
- Hook invocation at the right points
- Concurrent execution for safe tools
- Sequential execution for stateful tools
- Error handling and recovery
- Result aggregation

## Files Modified

### New Files
- `src/carbide/governance/__init__.py` - Public API exports
- `src/carbide/governance/permission.py` - Permission engine (170 lines)
- `src/carbide/governance/hooks.py` - Hook system (90 lines)
- `src/carbide/governance/bashguard.py` - AST analyzer (150 lines)
- `tests/test_permission.py` - Permission tests (149 lines)
- `tests/test_hooks.py` - Hook tests (110 lines)
- `tests/test_bashguard.py` - BashGuard tests (140 lines)

### Modified Files
- `src/carbide/tools/base.py` - Added RiskLevel import
- `src/carbide/tools/fs.py` - Added risk_level to all tools
- `src/carbide/tools/search.py` - Added risk_level to all tools
- `src/carbide/tools/shell.py` - Added risk_level to BashTool
- `src/carbide/tools/persistent_bash.py` - Added risk_level to PersistentBashTool

## Verification

```bash
# Import test
python -c "from carbide.governance import PermissionEngine, HookRegistry, BashGuard, RiskLevel, Severity; print('✓')"

# Quick governance demo
python examples/m2_integration.py

# Full test suite
python -m pytest tests/test_permission.py tests/test_hooks.py tests/test_bashguard.py -v
# Expected: 27 passed

# M1 + M2 together
python -m pytest tests/ -v
# Expected: 114 passed
```

---

**M2 治理层完成！所有 27 个测试通过。**
