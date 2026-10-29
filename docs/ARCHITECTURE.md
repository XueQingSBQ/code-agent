# Carbide Architecture

This document explains the design decisions and implementation details behind Carbide's layered architecture.

## Table of Contents

1. [Design Philosophy](#design-philosophy)
2. [Layer Overview](#layer-overview)
3. [Key Modules](#key-modules)
4. [Context Management](#context-management)
5. [Tool Execution](#tool-execution)
6. [Provider Abstraction](#provider-abstraction)
7. [Direct Subagents](#direct-subagents)
8. [Testing Strategy](#testing-strategy)

## Design Philosophy

### Measurable Claims Over Marketing

Carbide is built around two quantifiable properties:

1. **Context Efficiency**: A five-layer compression pyramid that reduces token consumption by 40%+ while maintaining task completion rates
2. **Fault Tolerance**: 95%+ session recovery rate under fault injection, with zero message history invariant violations

These aren't aspirational goals—they're measured in the test suite and SWE-bench evaluation.

### Harness vs Agency

The intelligence comes from the model. The harness contributes:
- How much context survives a long task
- Whether failures are recoverable
- How quickly tools dispatch
- What safety boundaries exist

Carbide focuses on these infrastructure concerns, not on prompt engineering or model behavior.

### Fail-Closed by Default

Unknown or ambiguous states default to the safe choice:
- Unknown tool → serialize execution
- Bash command we can't parse → ask for approval
- Context budget unclear → compress early
- Tool crash → synthesize error result, don't crash the session

## Layer Overview

```
┌─ surfaces/ ──────────────────────────────────────────────┐
│  cli/          Rich Live inline rendering                 │
│  sdk/          Agent: async context manager API           │
└──────────────────────────────────────────────────────────┘
┌─ engine/ ────────────────────────────────────────────────┐
│  loop.py       Main query loop (AsyncGenerator)           │
│  state.py      Frozen TurnState dataclass                 │
│  executor.py   Streaming parallel tool dispatcher         │
│  repair.py     History invariant enforcement              │
│  exits.py      Exit conditions and cleanup                │
└──────────────────────────────────────────────────────────┘
┌─ context/ ───────────────────────────────────────────────┐
│  L1 spill      Large results → disk                       │
│  L2 snip       Model-driven deletion                      │
│  L3 micro      Time-based pruning                         │
│  L4 compact    Full summarization                         │
│  L4.5 restore  Post-compact context restoration           │
│  manager.py    Cross-layer token accounting bus           │
└──────────────────────────────────────────────────────────┘
┌─ tools/ ─────────────────────────────────────────────────┐
│  base.py       Tool contract enforcement                  │
│  registry.py   Tool pool assembly + validation            │
│  fs/           read, write, edit, glob, grep              │
│  shell/        bash (persistent session)                  │
└──────────────────────────────────────────────────────────┘
┌─ governance/ ────────────────────────────────────────────┐
│  permission/   Rule matching + approval modes             │
│  hooks/        5 lifecycle events                         │
│  bashguard/    pattern-based shell safety analysis        │
│  audit/        Append-only decision log                   │
└──────────────────────────────────────────────────────────┘
┌─ providers/ + infra/ ────────────────────────────────────┐
│  providers/    Model provider protocol                    │
│  infra/        Config, logging, storage                   │
└──────────────────────────────────────────────────────────┘
```

### Dependency Rules

Dependencies flow **downward only**. Upper layers can import from lower layers, but not vice versa.

Enforced by `import-linter` in CI:
- Tools cannot import from engine
- Events cannot import from anything (dependency-free vocabulary)
- Only `providers/` may import vendor SDKs (`anthropic`)

Violating these rules causes CI to fail.

## Key Modules

### Engine: Query Loop

**File**: `src/carbide/engine/loop.py`

The core event loop is an `AsyncGenerator[Event, ExitReason]`:

```python
async def query_loop(
    messages: list[Message],
    provider: Provider,
    tools: ToolRegistry,
    state: TurnState,
) -> AsyncGenerator[Event, ExitReason]:
    while True:
        executor = StreamingToolExecutor(...)
        has_tool_calls = False

        # 1. Apply context compression if needed
        messages = await compress_if_needed(messages, state)

        # 2. Stream response and dispatch each complete tool call immediately
        async for chunk in provider.stream(messages):
            yield chunk
            if isinstance(chunk, StreamToolUse):
                has_tool_calls = True
                executor.add_tool(chunk)

        # 3. If no tool_use blocks → done
        if not has_tool_calls:
            return ExitReason.COMPLETE

        # 4. Collect results in dispatch order and continue
        results = await executor.gather_remaining()

        # 5. Append results and continue
        messages.extend(results)
```

**Key decision**: Tool dispatch happens **during** streaming, not after. The moment a `tool_use` JSON block closes, the tool starts executing. This shaves seconds off latency for multi-tool turns.

### Engine: Streaming Tool Executor

**File**: `src/carbide/engine/executor.py`

Tools run in parallel when safe:

```python
class StreamingToolExecutor:
    def add_tool(self, tool_call: StreamToolUse) -> None:
        # Safe tools wait only for the latest side-effect barrier.
        # Unsafe or unknown tools wait for all dispatched work and create a barrier.
        ...

    async def gather_remaining(self) -> list[CompletedTool]:
        # Harvest in dispatch order, independent of completion order.
        ...
```

**Concurrency rules** (fail-closed):
- `concurrency_safe=True`: Read, Glob, Grep → run in parallel
- `concurrency_safe=False`: Write, Edit, Bash → serialize
- Unknown tool → serialize

This is **not** `asyncio.TaskGroup` because TaskGroup cancels siblings on first failure. We need every tool to produce a result, even if it's an error.

### Engine: History Repair

**File**: `src/carbide/engine/repair.py`

The Anthropic API **requires** every `tool_use` to have a matching `tool_result`. If the session crashes mid-turn, the next API call is rejected.

Repair happens at session resume:

```python
def repair_orphan_tool_uses(messages: list[Message]) -> list[Message]:
    """Synthesize error results for orphaned tool_use blocks."""
    tool_use_ids = {block.id for msg in messages for block in msg.content if block.type == "tool_use"}
    result_ids = {block.tool_use_id for msg in messages for block in msg.content if block.type == "tool_result"}
    
    orphans = tool_use_ids - result_ids
    
    for orphan_id in orphans:
        messages.append(ToolResult(
            tool_use_id=orphan_id,
            content="Session interrupted",
            is_error=True,
        ))
    
    return messages
```

This invariant is tested with property-based tests (Hypothesis): generate random interruption points and verify repair always succeeds.

## Context Management

The five-layer pyramid is Carbide's main differentiator.

### L1: Spill (≥50KB single results)

Large tool outputs (e.g., `Read` on a 10K-line file) are written to disk, replaced with a preview + file path in the message history.

```
Original:  [tool_result] 200KB of code
After L1:  [tool_result] First 50 lines... [Full output: .carbide/spill/abc123.txt]
```

**Cost**: Zero API calls. **Savings**: ~150KB per spilled result.

### L2: Snip (model-driven)

The model gets a `snip` tool to explicitly remove messages by ID:

```json
{
  "name": "snip",
  "input": {"message_ids": ["msg_abc", "msg_def"]}
}
```

**Cost**: Zero API calls (tool execution only). **Savings**: Variable, model decides.

### L3: Micro-Compaction (time-based)

After 60 minutes, remove **re-fetchable** tool results older than the last 5 turns:
- ✓ Read, Glob, Grep → can re-run if needed
- ✗ Agent, Task → cannot recreate, keep forever

**Why 60 minutes?** Anthropic's prompt cache TTL is ~5 minutes. At 60 minutes, the cache is cold anyway, so pruning doesn't waste cached tokens.

**Cost**: Zero API calls. **Savings**: ~50K-100K tokens.

### L4: Full Compaction (budget exhausted)

When the context window is nearly full, summarize the entire conversation into a structured digest:

```markdown
## Summary (Turn 1-45)

**Task**: Fix authentication bug in users API

**Work completed**:
- Read src/auth.py, identified token validation issue
- Modified src/auth.py:42-56 to check expiry
- Tests passing: tests/test_auth.py

**Current state**:
- All changes committed
- 3 tests passing, 0 failing
- Ready for deployment
```

**Cost**: One API call to generate summary. **Savings**: 100K-200K tokens → 5K tokens.

### L4.5: Post-Compact Restoration

After compaction, the model often re-reads files it just worked on. Prevent this by **re-injecting**:
- Last 5 files read
- Active Skill content
- Current Plan (if in plan mode)

**Cost**: Zero API calls. **Savings**: Prevents redundant reads, ~20K-50K tokens.

### Cross-Layer Token Bus

Each layer reports tokens freed. Lower layers check the bus before acting:

```python
freed = L1_spill(messages)
if freed > threshold:
    return  # L2/L3 skip, enough freed already

freed += L2_snip(messages)
if freed > threshold:
    return  # L3/L4 skip

# ... continue down layers
```

This prevents "compressed twice" waste—the problem in s08 tutorial code where each strategy runs independently.

## Tool Execution

### Tool Contract

Every tool must declare three safety properties:

```python
class Tool(ABC):
    name: ClassVar[str]
    read_only: ClassVar[bool]           # Safe in plan mode?
    destructive: ClassVar[bool]         # Needs approval?
    concurrency_safe: ClassVar[bool]    # Can run in parallel?
```

**Enforcement**: Four layers (fail-fast):
1. `__init_subclass__` hook checks at import time
2. `registry.py` validates at assembly time
3. `mypy --strict` type checks
4. Test that enumerates all registered tools

Missing any property → import error, not runtime error.

### Example: Read Tool

```python
class ReadTool(Tool):
    name = "read"
    read_only = True        # Can use in plan mode
    destructive = False     # No approval needed
    concurrency_safe = True # Can run in parallel with other reads
    
    async def execute(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        # Implementation
        ...
```

### Bash Tool: Persistent Session

**File**: `src/carbide/tools/shell/bash.py`

The Bash tool maintains a **persistent subprocess** across tool calls:

```python
class BashSession:
    def __init__(self):
        self.proc = await asyncio.create_subprocess_shell(
            "bash",
            stdin=PIPE,
            stdout=PIPE,
            stderr=STDOUT,
        )
    
    async def run(self, command: str) -> str:
        self.proc.stdin.write(command.encode() + b"\n")
        await self.proc.stdin.drain()
        
        # Read until unique delimiter
        output = await self._read_until_prompt()
        return output
```

**Benefits**:
- `cd` changes persist across commands
- Environment variables persist
- ~100ms faster than spawning a new shell each time

**Risks**: Mitigated by BashGuard's pattern-based safety analysis (see Governance).

#### Background process ownership

Long-running commands are not allowed to disappear behind the persistent shell.
The Bash tool requires a background command to print its child PID explicitly,
for example:

```bash
python -m http.server 8000 --bind 127.0.0.1 \
  > .carbide/server.log 2>&1 & echo $!
```

`carbide.process.BackgroundJobManager` persists the PID, command, workspace,
owner session, detached flag, and log path as an atomic JSON record under
`.carbide/jobs/`. Attached jobs are stopped during `Agent.__aexit__()`; jobs
marked `detached=true` are left running and can be inspected with `carbide jobs`.
Stopping always checks the session owner, and uses `taskkill /T /F` on Windows
or a recursively discovered process tree on POSIX. Port scanning is deliberately
not used because a port does not prove process ownership. Stale cleanup is an
explicit, owner-scoped TTL operation; it never scans or kills another session's
jobs implicitly.

## Provider Abstraction

**File**: `src/carbide/providers/base.py`

The engine knows nothing about Anthropic specifically—it talks to a `Provider` protocol:

```python
class Provider(Protocol):
    async def stream(self, messages: list[Message]) -> AsyncIterator[StreamChunk]:
        """Stream a response."""
        ...
    
    async def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens for budget tracking."""
        ...
```

Only `src/carbide/providers/anthropic.py` imports the `anthropic` SDK. The rest of the codebase is vendor-agnostic.

### Custom API Endpoints

The Anthropic provider supports custom base URLs:

```python
provider = AnthropicProvider(
    api_key="your-key",
    base_url="https://your-endpoint.com",
    model="claude-sonnet-4-6",
)
```

**Implementation**: Just pass `base_url` to the Anthropic SDK—it handles authentication and streaming automatically for compatible endpoints.

**No custom headers needed**: The SDK sends `Authorization: Bearer <key>` by default, which most Anthropic-compatible proxies accept.

## Direct Subagents

Carbide uses one generic Worker implementation rather than role-specific child
Agents. The parent exposes one `agent` creation tool; it does not accept a
mode, role, or Todo item ID. A Worker reads the shared parent-session Todo
plan and calls `todo_claim` to atomically claim one dependency-ready task.

Each claim records the Worker ID, an expiry lease, and an item revision. The
Worker must present that ownership and revision when it calls `todo_update`.
The manager renews leases, requeues unfinished work after expiry, and clears
ownership after successful integration. `plan_version` remains a separate
optimistic concurrency check for structural plan changes such as adding tasks
or changing dependencies.

Workers receive the regular project tools and execute in an isolated detached
Git worktree when the project is a Git repository. Worktree commits are merged
under a lock. If another Worker has already advanced the parent branch, the
pending commit is rebased onto the latest parent HEAD before the merge; a real
conflict returns `merge_blocked`.

The semaphore limits concurrent Workers, and foreground calls can be promoted
to background execution after a timeout. The Worker conversation is stored in
its own session, while its Todo service points at the parent's durable session
database.

Parent-child communication uses a durable SQLite mailbox rather than sharing
the in-memory `messages` list:

```text
parent ── parent_to_child ──> child
parent <─ child_to_parent ─── child
          agent_messages (SQLite, queued → delivered → acknowledged)
```

The parent sends plan changes or answers with `agent_send`. A child reports
`progress` or asks a `question` with the child-only `agent_message` tool. A
background child may wait for an answer; a foreground child never blocks its
parent tool call and instead reports the question asynchronously. Messages are
claimed at model-request boundaries, so a stream or an in-flight tool is never
mutated halfway through execution.

Terminal child results deliberately have two projections: `final_text` is the
complete model-facing result, while `summary` is a bounded UI preview. A
background terminal result is also written to the parent mailbox, which lets a
parent resume after a poll or process restart without reconstructing child
memory.

### Subagent process leases

`agent_runs` stores an owner token and an absolute lease deadline. A
`SubagentManager` renews all of its active runs periodically. When a new
manager opens the same parent session, it reaps only expired
`queued`/`running`/`waiting_for_parent` records and marks them `orphaned`.
Completed records are never reaped. Status updates and child-to-parent result
messages include the owner token; after reaping, the old process cannot write
over the new durable state. This is deliberately lease-based rather than a
blind "mark every running row orphaned on resume" rule, so two live processes
do not steal each other's children.

## Testing Strategy

### Unit Tests

**Scope**: Individual components in isolation
- Tool schema validation
- Permission rule matching
- BashGuard parsing

**Example**: `tests/governance/test_bashguard.py` has 50+ adversarial cases:
```python
@pytest.mark.parametrize("cmd,expected", [
    ("echo $'\\x00'hello", "REJECT"),  # Null byte injection
    ("test -v 'a[$(id)]'", "REJECT"),   # Array subscript RCE
    ("timeout .5 eval 'id'", "REJECT"), # Wrapper bypass
])
def test_bashguard_adversarial(cmd, expected):
    assert bashguard.analyze(cmd) == expected
```

### Property-Based Tests

**Scope**: Invariants that must hold over all inputs
- Message history repair (every `tool_use` has a `tool_result`)
- Compression monotonicity (token count never increases)

**Tool**: Hypothesis

```python
from hypothesis import given, strategies as st

@given(st.lists(st.dictionaries(st.text(), st.text())))
def test_repair_preserves_invariant(messages):
    repaired = repair_orphan_tool_uses(messages)
    assert all_tool_uses_answered(repaired)
```

### Integration Tests

**Scope**: Multi-component workflows with mocked API
- Full query loop with simulated provider
- Compression pipeline across all layers
- Tool execution with success/failure/timeout

**MockProvider**: Returns scripted responses from fixtures.

### Chaos Tests

**Scope**: Fault injection
- 429 rate limits
- Network interruptions
- `prompt_too_long` errors
- Tool timeouts
- Process kills (SIGKILL mid-turn)

**Goal**: 95%+ recovery rate.

### E2E Tests

**Scope**: Real API calls (nightly only)
- Mark with `@pytest.mark.live`
- Excluded from default `pytest` run
- Run in CI via cron job

## Non-Goals

What Carbide **intentionally does not do**:

### MCP (Model Context Protocol)

Reason: Large integration surface, no contribution to core claims (context efficiency, fault tolerance).

### IDE Extensions / Web UI

Reason: Pure UI work. Demonstrates nothing about runtime engineering.

### Context Collapse (read-time projection)

Reason: Claude Code's internal feature, not publicly validated. Carbide leaves an interface hook but doesn't implement it.

## Performance Targets

These are the numbers Carbide is built to hit:

| Metric | Target | Measured By |
|--------|--------|-------------|
| Context efficiency | 40%+ token reduction | A/B test (pyramid on/off) on 30-task suite |
| Task completion rate | No degradation | Same suite, pyramid on/off |
| Fault recovery rate | ≥95% | Chaos test: 6 fault types × 20 runs each |
| Message history invariant | 0 violations | Property test: 1000 generated scenarios |
| SWE-bench Lite resolve rate | TBD | Full benchmark (300 instances) |
| Cost per instance | TBD | Logged during SWE-bench run |

These aren't aspirational—they're exit criteria for the project's completion.

## References

- [SWE-bench Paper](https://arxiv.org/abs/2310.06770)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- [tree-sitter](https://tree-sitter.github.io/)
- [Hypothesis Property Testing](https://hypothesis.readthedocs.io/)
