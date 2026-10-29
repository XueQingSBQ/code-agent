# Carbide Development Milestones

## ✅ M1: Tool Layer (Complete - 2026-09-16)

**87 tests passing**

### Components
- **File Tools**: `ReadTool`, `WriteTool`, `EditTool`
- **Search Tools**: `GlobTool`, `GrepTool`
- **Shell Tools**: `BashTool`, `PersistentBashTool`
- **Session Management**: `BashSession` for stateful shell interactions
- **Tool Contract**: Enforced base class with safety attributes

### Features
- Read-only operations (glob, grep, read)
- File modifications (write, edit)
- Shell command execution (bash)
- Persistent bash sessions maintaining state across commands
- Ripgrep integration with Python fallback
- Path escaping and workspace containment
- Output truncation and size limits

### Test Coverage
- `test_tool_contract.py` - 7 tests (Tool base class contract)
- `test_bash_session.py` - 12 tests (Persistent sessions)
- `test_tools.py` - 41 tests (File operations, 14 platform-skipped)
- `test_persistent_bash_tool.py` - 7 tests (Persistent bash tool)
- `test_search.py` - 20 tests (Glob and grep)

### Documentation
- [M1_COMPLETION.md](docs/M1_COMPLETION.md)
- [M1_TOOLS_GUIDE.md](docs/M1_TOOLS_GUIDE.md)

---

## ✅ M2: Governance Layer (Complete - 2026-09-16)

**27 tests passing** (111 total with M1)

### Components
- **Permission Engine**: Risk-based tool execution gating
- **Hook System**: Before/after execution interception
- **BashGuard**: AST-based shell command safety analysis

### Features

#### Permission Engine (`permission.py`)
- Three modes: AUTO (safe auto-allow, risky prompt), ALWAYS (allow all), NEVER (block all)
- Three risk levels: SAFE (read-only), WRITE (file mods), DESTRUCTIVE (dangerous ops)
- Runtime mode switching
- User prompt generation for blocked operations

#### Hook System (`hooks.py`)
- Before hooks can observe or short-circuit execution
- After hooks can inspect or modify results
- Multiple hooks chain correctly
- Synchronous by design (no latency overhead)

#### BashGuard (`bashguard.py`)
- Detects recursive deletes (`rm -rf`)
- Detects dangerous paths (`/`, `/usr`, `/etc`)
- Detects destructive redirects (`>`, `>>`)
- Detects nested command substitutions
- Detects piped dangerous commands
- Three severity levels: INFO, WARNING, CRITICAL

### Test Coverage
- `test_permission.py` - 9 tests (Permission engine modes)
- `test_hooks.py` - 6 tests (Hook registration and chaining)
- `test_bashguard.py` - 12 tests (Command pattern detection)

### Integration
All tools now carry `risk_level` ClassVar:
```python
ReadTool.risk_level = RiskLevel.SAFE
WriteTool.risk_level = RiskLevel.WRITE
BashTool.risk_level = RiskLevel.DESTRUCTIVE
```

### Documentation
- [M2_COMPLETION.md](docs/M2_COMPLETION.md)
- [M2_QUICKSTART.md](docs/M2_QUICKSTART.md)
- [examples/m2_integration.py](examples/m2_integration.py)

---

## 🚧 M3: Executor (Planned)

Tool orchestration with governance integration.

### Planned Components
- **Executor**: Orchestrates tool execution with permission checks and hooks
- **Concurrency**: Parallel execution for safe tools, sequential for stateful
- **Error Recovery**: Graceful failure handling and result aggregation
- **Tool Provider**: Registry and discovery

### Goals
- Permission engine gates execution before tool runs
- Hooks fire at the right pipeline points
- Safe tools run in parallel (glob + grep + read)
- Stateful tools run sequentially (bash sessions)
- Every tool_use gets a tool_result (fault tolerance)

### Test Strategy
- Unit tests for executor logic
- Integration tests with real tools
- Concurrency tests with parallel execution
- Error injection tests for recovery paths
- Mock provider for deterministic testing

---

## 🚧 M4: Context Pyramid (Planned)

Five-layer token management system.

### Planned Components
- **Layer 1**: Single oversized tool result → disk
- **Layer 2**: Batch of tool results → summary
- **Layer 3**: Old turns → summary
- **Layer 4**: Recent turns → compression
- **Layer 5**: Full compaction with LLM summary

### Goals
- Stay within context budget without hard failures
- Each layer reports tokens freed
- No double-compaction of same content
- Preserve critical information through compaction
- Measure context efficiency across long tasks

### Test Strategy
- Unit tests per layer
- Integration tests with mock messages
- Property tests for token accounting
- Long-session simulation tests

---

## 🎯 Current Status

**M1 + M2 Complete**: Foundation is solid
- 6 tools with enforced contracts
- Persistent shell sessions
- Permission-based execution gating
- Hook system for extensibility
- AST-based command safety analysis
- 111 tests passing (97 passed, 14 platform-skipped)

**Next**: M3 Executor to wire everything together

---

## Verification Commands

```bash
# M1 tests
python -m pytest tests/test_tool_contract.py tests/test_bash_session.py tests/test_tools.py tests/test_persistent_bash_tool.py -v

# M2 tests
python -m pytest tests/test_permission.py tests/test_hooks.py tests/test_bashguard.py -v

# Full M1 + M2
python -m pytest tests/ -v

# Import verification
python -c "from carbide.tools import ReadTool, WriteTool, EditTool, GlobTool, GrepTool, BashTool, PersistentBashTool; print('M1 ✓')"
python -c "from carbide.governance import PermissionEngine, HookRegistry, BashGuard, RiskLevel, Severity; print('M2 ✓')"

# Integration example
python examples/m2_integration.py
```

---

## Design Principles

1. **Fail-closed**: Unknown tools are treated as destructive and sequential
2. **Fault tolerance**: Every tool_use gets a tool_result, even on crash
3. **Context efficiency**: Measured, not asserted - token budgets enforced
4. **Concurrency**: Safe tools run parallel, stateful tools sequential
5. **Governance**: Permission checks before execution, hooks for extensibility
6. **Testing**: Property-based for invariants, unit for logic, integration for flows
