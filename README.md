# Carbide

A coding agent harness built around two claims that can be measured rather than asserted:
**context efficiency** and **fault tolerance**.

The agency comes from the model. What a harness contributes is everything around it — how much
of the context window survives a long task, and whether a run that hits a failure recovers or
dies. Carbide is an attempt to build that layer properly, with the numbers to back it.

> Status: **Experimental Agent Harness; production validation in progress**.
> - Latest recorded offline regression run (2026-10-26): 626 passed, 41 live tests deselected.
> - Fixed-protocol reliability study: 100/140 before fixes, 140/140 after fixes;
>   the following runtime revision passed a separate 21/21 smoke run.
> - Controlled eight-stage long-context A/B: cumulative Provider input tokens reduced by
>   41.1%, with L1 compression active. This is not a cost-saving or general task-success claim.
> - Latest real-issue diagnostic study: 4/16 attempts passed independent grading on two
>   selected issues; it does not establish a general coding success rate.
> - Session resume/repair, governance, persistent Bash, memory extraction and multi-agent
>   coordination have regression coverage. Production task correctness remains an open gate.

See the [verification study](docs/VERIFICATION_STUDY_20261026.md),
[reliability report](docs/READINESS_FIX_VALIDATION_20261025.md),
[compression report](docs/CONTEXT_COMPRESSION_REQUEST_BOUNDARY_EXPERIMENT.md), and
[real-issue study](docs/VERIFICATION_STUDY_6LUNA_20261026.md) for protocols and limitations.
Raw evaluation checkouts, request transcripts, local configuration and personal drafts are
not published in this repository; the evaluation scripts describe how to generate new results.

## Why it exists

Most agent frameworks treat the context window as someone else's problem: they append to a
message list until the API refuses, then summarise everything and lose the thread. Carbide
treats it as a budget with five graduated responses, from spilling one oversized tool result to
disk up to a full summarising compaction — each layer reporting what it freed so the next one
does not compact the same tokens twice.

The reliability half is the same idea applied to failure. Every `tool_use` block gets a
`tool_result`, even when the tool crashed, the connection dropped, or the user hit Ctrl+C
mid-turn. Without that invariant the *next* request is rejected outright, which is how a long
session dies twenty minutes in.

## Architecture

```
cli/          Rich Live rendering, inline (not a full-screen TUI)
sdk.py        Agent: async context manager yielding typed events
engine/       query loop, streaming tool executor, turn state, history repair
context/      the five-layer pyramid and its cross-layer token bus
governance/   permission engine, hooks, pattern-based bashguard (M2 ✓)
tools/        read write edit glob grep bash, behind one enforced contract (M1 ✓)
providers/    the model seam: stream() and count_tokens(), nothing else
infra/        config precedence chain, logging
memory/       durable memory extraction, validation, recall, and persistence
```

Two design decisions worth naming:

**Tools dispatch mid-stream.** A tool starts the moment its `tool_use` JSON closes, not when
the message ends. Read/Glob/Grep run in parallel; anything that mutates state acts as a
barrier. The concurrency flag is fail-closed — an unknown or undeclared tool is serial.

**Not `asyncio.TaskGroup`.** A TaskGroup cancels its siblings when one task raises, which is
precisely wrong here: a crashed tool must still produce an error result, and so must the tools
running next to it.

## Try it

```bash
uv sync
```

```bash
cp .env.example .env  # then add your ANTHROPIC_API_KEY
```

```bash
uv run carbide "what does this project do?"
```

Run it with no prompt for an interactive session. `-C/--workspace` points it at another
directory, `--no-context-pyramid` disables context management for A/B comparison. On Windows,
the `gitbash` shell backend requires Git Bash; destructive commands ask for `y/N` approval.

```bash
# One-shot or interactive query
uv run carbide -C . "explain the project structure"
uv run carbide -C .

# Limit the run and show thinking/timing details
uv run carbide --max-turns 20 --verbose "run the test suite"

# Inspect and resume durable sessions
uv run carbide list-sessions -C .
uv run carbide list-sessions -C . --status failed
uv run carbide --resume <session-id> "continue the unfinished work"

# Inspect and control Bash background jobs
uv run carbide jobs -C .
uv run carbide jobs status -C . --session <session-id>
uv run carbide jobs stop <job-id> -C . --session <session-id>
uv run carbide jobs stop-all -C . --session <session-id>
uv run carbide jobs cleanup -C . --session <session-id> --older-than 86400

# Check, back up, and restore the session database
uv run carbide check-sessions -C .
uv run carbide migrate-sessions -C .
uv run carbide backup-sessions -C . --output ./sessions-backup.db
uv run carbide restore-sessions -C . --input ./sessions-backup.db --overwrite
```

The CLI streams assistant text, turn boundaries, tool starts/completions, compression, memory
updates, and the terminal reason. A terminal reason other than `completed` is reported as a
degraded/failed run rather than being silently presented as success. Background Bash commands
must print their child PID with `echo $!`; Carbide persists the job under `.carbide/jobs/`,
reclaims attached jobs when the owning Agent exits, and preserves only jobs explicitly marked
`detached`.

### Durable memory

Memory is separate from the session store: sessions retain conversation history, while
Memory stores only stable user preferences, confirmed feedback, project decisions, and
useful references. Extraction runs after a successful final answer and is bounded so a
memory-provider failure cannot fail the main query. Credentials and other likely secrets
are rejected before they can be written.

Choose the persistence policy from the CLI:

```bash
# Never extract or write durable memories
uv run carbide --memory-write-mode off "analyze this code"

# Only persist facts explicitly requested by the user
uv run carbide --memory-write-mode explicit "remember that this project uses pytest"

# Show candidate names to the caller; confirm with the SDK before writing
uv run carbide --memory-write-mode suggest "remember that this project uses pytest"

# Explicit requests or high-confidence candidates are persisted (default)
uv run carbide --memory-write-mode auto "remember that this project uses pytest"
```

The default can also be set in `.env` with `CARBIDE_MEMORY_WRITE_MODE=auto`. In `suggest`
mode, a custom UI can confirm the names from the `memory_update` event and call
`agent.memory_manager.persist_suggestions(names)`; lower-level integrations that already
hold the candidate objects can use `persist_candidates(candidates, turn)` instead.

### Agent skills and project rules

Skills are capabilities of the Carbide Agent, so they are stored outside the active project by
default:

```text
~/.carbide/skills/
```

On Windows this normally resolves to `C:\Users\<user>\.carbide\skills\`. Override it with
`CARBIDE_HOME`, `CARBIDE_SKILLS_DIR`, or the SDK's `agent_skills_dir` argument. The project's
`.carbide/` directory is reserved for project state such as memory, rules, audit logs, spill
files, and sessions; `.carbide/skills/` is not automatically loaded as Agent capability.

Skills are discovered as lightweight name/description metadata. The model can autonomously load
one with the governed `load_skill` tool when the task matches its description:

```text
load_skill({"name": "code-review"})
```

Users can also activate one explicitly in the conversation with its slash name, for example:

```text
carbide> /code-review review the changes in src/carbide/engine/loop.py
```

The full skill content is loaded only after the registered skill name is addressed, keeping the
base prompt small and preserving prompt-cache stability.

### Direct subagents

The parent Agent creates generic Todo Workers through the governed `agent` tool. The tool does
not accept a role, mode, or Todo item ID. Each Worker reads the shared parent-session plan and
uses `todo_claim` to atomically claim one dependency-ready task. It then executes with the normal
project tools, updates the task with `todo_update`, and reports its result through
`agent_message`.

Workers use an isolated detached Git worktree when the project is a Git repository. Their task
leases are renewed while they run and are requeued when a Worker lease expires. Worktree commits
are merged under a lock; a later Worker rebases onto the latest parent HEAD before merging, and
real conflicts return `merge_blocked` for explicit handling.

Communication is durable and bidirectional through the parent session database:

```text
parent -- agent_send / parent_to_child --> child
parent <-- agent_message / child_to_parent -- child
```

The model can start a child with `agent`, inspect it with `agent_status`, send a plan update or
answer with `agent_send`, and stop it with `agent_cancel`. Background children publish their full
terminal result to the parent mailbox; the UI-facing `summary` remains bounded. SDK integrations
can read completed results without waiting:

```python
updates = agent.poll_subagent_updates()
status = agent.get_subagent_status(agent_id)
agent.send_subagent_message(agent_id, kind="plan_update", content="Prioritize tests first.")
```

Messages are injected only before a new model request. This preserves the active stream and any
in-flight tool execution while still allowing the parent to change a child's plan.

The parent creates and adjusts the global plan, but does not assign individual Todo items to
Workers. Task-level ownership uses `owner_id`, `lease_until`, and an item revision; plan-level
changes still use `plan_version` for optimistic concurrency control.

Each running child is owned by a process-scoped lease. The manager renews active leases in the
background; a later process only marks queued/running/waiting children as `orphaned` after their
lease expires. State and mailbox writes are fenced by the owner token, so an old process that
returns after recovery cannot overwrite the orphaned record.

### Runtime and audit configuration

Values in the process environment override the project `.env` file, so the same configuration
works in local shells, CI, and containers. The audit log is written under
`.carbide/audit/<session-id>.jsonl`, rotated before it reaches 10 MB, and keeps five backups by
default. Tool arguments and results are redacted/truncated before being persisted.

```bash
export CARBIDE_AUDIT_MAX_BYTES=10000000
export CARBIDE_AUDIT_BACKUP_COUNT=5
export CARBIDE_AUDIT_MAX_FIELD_CHARS=16384
```

The session database is `.carbide/sessions.db`. `backup-sessions` uses SQLite's online backup API;
`restore-sessions` validates the SQLite integrity and required tables before atomically replacing
the destination. Existing backups/databases are preserved unless `--overwrite` is supplied.

As a library:

```python
from pathlib import Path

from carbide import Agent
from carbide.events import TextDelta, ToolCallFinished

async with Agent(project_dir=Path(".")) as agent:
    async for event in agent.query("fix the failing test in tests/test_parser.py"):
        match event:
            case TextDelta(text):
                print(text, end="")
            case ToolCallFinished(name=name, is_error=failed):
                print(f"\n[{name}] {'failed' if failed else 'ok'}")
```

## Evaluation

Carbide includes a SWE-bench Lite evaluation harness for measuring real-world performance on GitHub issues from popular Python repositories.

### Pilot Results

Simplified pilot evaluation (3 instances, no Docker isolation):
- **Completion rate**: 100% (3/3)
- **Cost**: $0.004 total (~$0.0014/instance)
- **Average time**: 3.5s/instance
- **Token usage**: 254 tokens/instance average

See [docs/PILOT_EVALUATION_SUMMARY.md](docs/PILOT_EVALUATION_SUMMARY.md) for details.

### Context compression result

A controlled live A/B experiment with five paired runs measured L1 spill on three 60,000-character
tool results. Provider `count_tokens` decreased from 45,508 to 2,089 tokens, a 95.410% reduction,
while the exact final-response check completed 5/5 with compression and 5/5 without it. This is
an L1 controlled result, not a claim about every workload or the full L1-L4.5 pyramid. See
[docs/CONTEXT_COMPRESSION_EXPERIMENT.md](docs/CONTEXT_COMPRESSION_EXPERIMENT.md) and the raw
[JSON report](eval_results/context-efficiency/controlled-ab-final.json).

The long-task experiment keeps one Agent alive for eight sequential phases,
with two real model requests and a 56,000-character tool result per phase.
Across two paired runs, exact Provider request tokens fell from 907,448 to
583,976 per run on average, a 35.646% cumulative reduction; peak single-request
context fell 24.956%, and all phases completed 2/2 with and without compression.
See [the long-task report](docs/CONTEXT_COMPRESSION_LONG_TASK_EXPERIMENT.md) and
the [raw long-task JSON](eval_results/context-efficiency/long-task-ab.json).

The request-boundary fix was re-tested with the same eight-phase workload parameters:
the count_tokens sum fell 44.440% (41.086% by streamed usage). A separate twelve-phase
coding evaluation exercised L1, L2, L3, L4 and file restoration across 27 selected runs;
26 tasks passed the complete checkpoint and code checks. Failed and superseded runs
remain in the raw reports. See the [boundary comparison](docs/CONTEXT_COMPRESSION_REQUEST_BOUNDARY_EXPERIMENT.md)
and [layered evaluation](docs/CONTEXT_COMPRESSION_LAYERED_EXPERIMENT.md) for measurements,
cache tradeoffs and the remaining completion-verification failure.

### Running Evaluations

```bash
# Simplified pilot (no Docker, quick validation)
python evals/run_pilot.py

# Full evaluation with Docker isolation
python -m evals.swebench.cli \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results \
    --api-key $ANTHROPIC_API_KEY \
    --model claude-sonnet-4-20250514 \
    --max-instances 10
```

### Custom API Endpoints

Carbide supports Anthropic-compatible API endpoints:

```bash
export ANTHROPIC_API_KEY=your-key
export ANTHROPIC_BASE_URL=https://your-endpoint.com
python evals/run_pilot.py
```

**Metrics reported:**
- Completion rate
- Average cost per instance (USD)
- Average token consumption
- Time per instance

See [evals/swebench/README.md](evals/swebench/README.md) for full documentation and [docs/PILOT_RESULTS.md](docs/PILOT_RESULTS.md) for detailed pilot analysis.

### Real-repository context experiments

The [repository evaluation workflow](evals/context_efficiency/README.md) compares
compression off, the default 200K budget and a separate 32K pressure profile on
frozen Flask/SymPy issues. Independent grading checks production patches against
designated regressions; service failures, invalid Usage and interrupted attempts
are reported separately. This native local subset is not an official SWE-bench
leaderboard result. Results and completeness are recorded in the
[repository report](docs/CONTEXT_COMPRESSION_REPOSITORY_EXPERIMENT.md) and
[combined maintenance report](docs/CONTEXT_COMPRESSION_REPOSITORY_SESSION_EXPERIMENT.md).

## Development

```bash
uv run ruff check . && uv run mypy && uv run pytest
```

The current offline release gate is:

```bash
uv run pytest -q
uv run pytest -q -W error::pytest.PytestUnraisableExceptionWarning
uv run ruff check src/carbide --output-format concise
uv run ruff format --check src/carbide
uv run mypy src/carbide
uv run lint-imports
uv build
```

These checks cover deterministic behavior only. Live Anthropic-compatible endpoint validation,
long-running soak measurements, performance baselines, Docker isolation, and production log
aggregation still require an environment with the relevant service or deployment infrastructure;
they are tracked in [PRODUCTION_READINESS_STATUS.md](PRODUCTION_READINESS_STATUS.md).

The test suite is layered by what each kind of bug looks like. Property-based tests
(Hypothesis) cover the history-repair invariant, because "every `tool_use` is answered" is a
claim about *all* histories, not a handful of examples. The scripted `MockProvider` covers the
query loop's failure ladders deterministically — truncation recovery, provider errors, streams
that end without a terminal event.

## Custom API Endpoints

Carbide can work with any Anthropic-compatible API endpoint. Configure via environment variables:

```bash
# .env file
ANTHROPIC_API_KEY=your-key
ANTHROPIC_BASE_URL=https://your-endpoint.com
ANTHROPIC_MODEL=claude-sonnet-4-6
```

Or programmatically:

```python
from pathlib import Path

from carbide.sdk import Agent

agent = Agent(
    api_key="your-key",
    base_url="https://your-endpoint.com",
    model="claude-sonnet-4-6",
)
```

The Provider layer uses the standard Anthropic SDK, which handles authentication and streaming automatically for compatible endpoints.

## License

MIT
