"""CLI entry point.

Two modes: one-shot (``carbide "fix the test"``) and interactive REPL. Both are
thin consumers of ``Agent.query`` — the CLI holds no agent logic of its own.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.table import Table
from rich.text import Text

from carbide import __version__
from carbide.cli.render import Renderer
from carbide.events import Done, ExitReason, TextDelta, ToolCallStarted, TurnCompleted
from carbide.infra.config import Config
from carbide.process import (
    BackgroundJob,
    BackgroundJobManager,
    JobNotFoundError,
    JobOwnershipError,
    JobRepository,
)
from carbide.providers.base import Provider, StreamToolUse
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry

_APPROVAL_COMMAND_LIMIT = 240


def _approval_prompt(call: StreamToolUse, reason: str) -> str:
    """Build a compact approval prompt with the command being authorized."""
    command = call.arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return f"\n[permission] {call.name}: {reason} [y/N] "

    preview = " ".join(command.split())
    if len(preview) > _APPROVAL_COMMAND_LIMIT:
        preview = preview[: _APPROVAL_COMMAND_LIMIT - 3] + "..."
    return f"\n[permission] {call.name}\n  command: {preview}\n  {reason} [y/N] "


def _session_summary(value: object, *, limit: int = 72) -> str:
    """Keep a session list row compact and single-line."""

    text = " ".join(str(value or "未命名会话").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carbide",
        description="A coding agent harness with measurable context efficiency.",
    )

    # Add subparsers for commands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Main query command (default)
    query_parser = subparsers.add_parser("query", help="Run a query (default)", add_help=False)
    query_parser.add_argument("prompt", nargs="*", help="Task to run. Omit for interactive mode.")
    query_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )
    query_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show thinking and timings."
    )
    query_parser.add_argument(
        "--no-context-pyramid",
        action="store_true",
        help="Disable context compaction (for A/B measurement).",
    )
    query_parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Optional hard turn limit (default: unlimited).",
    )
    query_parser.add_argument(
        "--resume", type=str, default=None, help="Resume session by session_id."
    )
    query_parser.add_argument(
        "--memory-write-mode",
        choices=("off", "explicit", "suggest", "auto"),
        default=None,
        help="Memory persistence policy (default: config/env auto).",
    )

    # List sessions command
    list_parser = subparsers.add_parser("list-sessions", help="List all stored sessions")
    list_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )
    list_parser.add_argument(
        "--status",
        choices=("running", "completed", "failed"),
        default=None,
        help="Filter by status.",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of sessions to show per page (default: 20).",
    )
    list_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Number of matching sessions to skip (default: 0).",
    )
    list_parser.add_argument(
        "--search",
        default=None,
        help="Search session IDs, workspaces, and conversation messages.",
    )

    jobs_parser = subparsers.add_parser("jobs", help="Inspect or stop managed background processes")
    jobs_parser.add_argument(
        "action",
        nargs="?",
        choices=("status", "stop", "stop-all", "cleanup"),
        default="status",
        help="Operation to perform (default: status).",
    )
    jobs_parser.add_argument("job_id", nargs="?", help="Job ID for the stop operation.")
    jobs_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )
    jobs_parser.add_argument(
        "--session",
        default=None,
        help="Filter jobs or authorize stop operations for one agent session.",
    )
    jobs_parser.add_argument(
        "--include-detached",
        action="store_true",
        help="Include explicitly detached jobs in stop-all.",
    )
    jobs_parser.add_argument(
        "--older-than",
        type=float,
        default=86_400.0,
        help="Cleanup threshold in seconds (default: 86400).",
    )

    backup_parser = subparsers.add_parser("backup-sessions", help="Back up the session database")
    backup_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )
    backup_parser.add_argument("-o", "--output", type=Path, required=True, help="Backup file path.")
    backup_parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing backup file."
    )

    check_parser = subparsers.add_parser("check-sessions", help="Check session database integrity")
    check_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )

    migrate_parser = subparsers.add_parser(
        "migrate-sessions", help="Apply compatible session database migrations"
    )
    migrate_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )

    restore_parser = subparsers.add_parser(
        "restore-sessions", help="Restore a validated session database backup"
    )
    restore_parser.add_argument(
        "-C", "--workspace", type=Path, default=None, help="Working directory."
    )
    restore_parser.add_argument("--input", type=Path, required=True, help="Backup file path.")
    restore_parser.add_argument(
        "--overwrite", action="store_true", help="Replace the current session database."
    )

    # For backward compatibility, also accept arguments without subcommand
    parser.add_argument("prompt", nargs="*", help="Task to run. Omit for interactive mode.")
    parser.add_argument("-C", "--workspace", type=Path, default=None, help="Working directory.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show thinking and timings.")
    parser.add_argument(
        "--no-context-pyramid",
        action="store_true",
        help="Disable context compaction (for A/B measurement).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Optional hard turn limit (default: unlimited).",
    )
    parser.add_argument("--resume", type=str, default=None, help="Resume session by session_id.")
    parser.add_argument(
        "--memory-write-mode",
        choices=("off", "explicit", "suggest", "auto"),
        default=None,
        help="Memory persistence policy (default: config/env auto).",
    )
    parser.add_argument("--version", action="version", version=f"carbide {__version__}")

    return parser


def _render_memory_result(renderer: Renderer, data: dict[str, Any]) -> None:
    """Render both queued and completed background-memory states."""
    status = data.get("status")
    if status == "queued":
        renderer.console.print("[dim]  memory queued[/dim]")
        return

    saved = data.get("saved", [])
    updated = data.get("updated", [])
    suggested = data.get("suggested", [])
    errors = data.get("errors", [])
    if saved:
        renderer.console.print(f"[dim]  memory saved: {', '.join(saved)}[/dim]")
    if updated:
        renderer.console.print(f"[dim]  memory updated: {', '.join(updated)}[/dim]")
    if suggested:
        renderer.console.print(f"[dim]  memory suggestions: {', '.join(suggested)}[/dim]")
    if errors:
        renderer.console.print(f"[yellow]  memory warning: {errors[-1]}[/yellow]")
    if status == "empty":
        renderer.console.print("[dim]  memory extraction returned no candidates[/dim]")


def _render_polled_memory_updates(agent: Any, renderer: Renderer) -> None:
    """Render completed background jobs when the host exposes polling."""
    poll = getattr(agent, "poll_memory_updates", None)
    if not callable(poll):
        return
    for update in poll():
        if isinstance(update, dict):
            _render_memory_result(renderer, update)


def _subagent_preview(value: object, *, limit: int = 240) -> str:
    """Render only a bounded child summary, never the full child transcript."""
    if isinstance(value, dict):
        result = value.get("result")
        result_text = result.get("final_text", "") if isinstance(result, dict) else ""
        value = value.get("summary") or result_text
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _render_subagent_message(renderer: Renderer, data: dict[str, Any]) -> None:
    """Render one child-to-parent mailbox message at a model boundary."""
    kind = str(data.get("kind", "message"))
    preview = _subagent_preview(data.get("content"))
    renderer.console.print(f"[dim]  ← subagent {kind}: {preview}[/dim]")


def _render_polled_subagent_updates(agent: Any, renderer: Renderer) -> None:
    """Render terminal results completed while no parent request was active."""
    poll = getattr(agent, "poll_subagent_updates", None)
    if not callable(poll):
        return
    for update in poll():
        if isinstance(update, dict):
            agent_id = str(update.get("agent_id", "unknown"))[:8]
            status = str(update.get("status", "unknown"))
            preview = _subagent_preview(update)
            renderer.console.print(f"[dim]  ← subagent {agent_id} {status}: {preview}[/dim]")


async def run_once(
    agent: Agent, renderer: Renderer, prompt: str, tools: list[dict[str, Any]] | None = None
) -> None:
    from carbide.cli.todo import handle_todo_command

    _render_polled_subagent_updates(agent, renderer)
    todo_service = getattr(agent, "todo_service", None)
    if todo_service is not None and handle_todo_command(prompt, todo_service, renderer.console):
        return
    renderer.reset_usage()
    turn_count = 0
    final_reason = ExitReason.COMPLETED

    def parse_exit_reason(value: object) -> ExitReason:
        """Convert an engine reason into the public CLI exit vocabulary."""
        try:
            return ExitReason(str(value))
        except ValueError:
            return ExitReason.PROVIDER_ERROR

    async for event in agent.query(prompt, tools=tools):
        # Convert QueryEvent to CLI events
        if event.type == "chunk" and event.data:
            # Only handle content deltas
            if event.data.type == "content_block_delta" and event.data.content:
                renderer.handle(TextDelta(text=event.data.content))
        elif event.type == "turn_started" and isinstance(event.data, dict):
            turn = event.data.get("turn")
            if turn is not None:
                renderer.console.print(f"[dim]  turn {turn} started[/dim]")
        elif event.type == "tool_use" and isinstance(event.data, dict):
            arguments = event.data.get("input", {})
            renderer.handle(
                ToolCallStarted(
                    tool_use_id=str(event.data.get("id", "unknown")),
                    name=str(event.data.get("name", "unknown")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        elif event.type == "tool_complete":
            # Show tool execution
            tool_name = event.data.get("tool_name", "unknown")
            duration = event.data.get("duration_ms", 0)
            is_error = event.data.get("is_error", False)
            status = "❌ failed" if is_error else "✓ completed"
            renderer.console.print(f"[dim]  → {tool_name} {status} ({duration:.1f}ms)[/dim]")
        elif event.type == "todo_update" and isinstance(event.data, dict):
            content = event.data.get("content")
            if isinstance(content, str) and content:
                renderer.console.print("[dim]  Todo state[/dim]")
                renderer.console.print(content, markup=False)
        elif event.type == "subagent_message" and isinstance(event.data, dict):
            _render_subagent_message(renderer, event.data)
        elif event.type == "budget_warning" and isinstance(event.data, dict):
            remaining = []
            if event.data.get("remaining_calls") is not None:
                remaining.append(f"{event.data['remaining_calls']} model calls left")
            if event.data.get("remaining_seconds") is not None:
                remaining.append(f"{event.data['remaining_seconds']:.0f}s left")
            renderer.console.print("Budget: " + " · ".join(remaining), style="dim", markup=False)
        elif event.type in ("memory_queued", "memory_update") and isinstance(event.data, dict):
            if event.type == "memory_queued":
                _render_memory_result(renderer, {"status": "queued"})
            else:
                _render_memory_result(renderer, event.data)
        elif event.type == "compression" and isinstance(event.data, dict):
            layers = ", ".join(str(layer) for layer in event.data.get("layers", []))
            freed = int(event.data.get("tokens_freed", 0))
            renderer.console.print(f"[dim]  compression {layers}: freed {freed:,} tokens[/dim]")
        elif event.type == "turn_complete":
            turn_count += 1
            # Convert to TurnCompleted event for renderer
            if isinstance(event.data, dict):
                turn_completed = TurnCompleted(
                    turn=event.data.get("turn", turn_count),
                    input_tokens=event.data.get("input_tokens", 0),
                    output_tokens=event.data.get("output_tokens", 0),
                    cache_read_tokens=event.data.get("cache_read_tokens", 0),
                    cache_creation_tokens=event.data.get("cache_creation_tokens", 0),
                )
                renderer.handle(turn_completed)
                if event.data.get("reason"):
                    final_reason = parse_exit_reason(event.data["reason"])
        elif event.type == "error":
            if isinstance(event.data, dict):
                final_reason = parse_exit_reason(event.data.get("reason", "provider_error"))
            else:
                final_reason = ExitReason.PROVIDER_ERROR
            renderer.handle(Done(reason=final_reason, turns=turn_count or 1))
            return

    _render_polled_memory_updates(agent, renderer)
    _render_polled_subagent_updates(agent, renderer)

    # QueryLoop reports its terminal reason in the final turn_complete event;
    # never overwrite a limit or degraded completion with COMPLETED.
    renderer.handle(Done(reason=final_reason, turns=turn_count or 1))


async def run_interactive(
    agent: Agent,
    renderer: Renderer,
    console: Console,
    tools: list[dict[str, Any]] | None = None,
) -> None:
    history_path = Path.home() / ".carbide_history"
    session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))
    console.print("[dim]carbide — Ctrl-D to exit[/dim]")
    while True:
        try:
            prompt = await session.prompt_async("carbide> ")
        except (EOFError, KeyboardInterrupt):
            flush = getattr(agent, "flush_memory", None)
            if callable(flush):
                for update in await flush():
                    if isinstance(update, dict):
                        _render_memory_result(renderer, update)
            else:
                _render_polled_memory_updates(agent, renderer)
            console.print("[dim]bye[/dim]")
            return
        if not prompt.strip():
            continue
        try:
            await run_once(agent, renderer, prompt, tools=tools)
        except KeyboardInterrupt:
            # Cancelling one task must not end the session; the engine has
            # already synthesised results for any in-flight tools.
            console.print("\n[yellow]interrupted[/yellow]")


async def run_repl(
    provider: Provider,
    project_dir: Path,
    enable_compression: bool = True,
    enable_memory: bool = True,
    enable_skills: bool = True,
    enable_rules: bool = True,
) -> None:
    """Compatibility wrapper for the original SDK-driven REPL entry point."""

    registry = create_default_registry()
    agent = Agent(
        provider=provider,
        project_dir=project_dir,
        tool_registry=registry,
        enable_compression=enable_compression,
        enable_memory=enable_memory,
        enable_skills=enable_skills,
        enable_rules=enable_rules,
    )
    console = Console()
    renderer = Renderer(console)
    try:
        await run_interactive(agent, renderer, console, tools=registry.api_schemas())
    finally:
        await agent.__aexit__(None, None, None)


async def list_sessions(
    workspace: Path | None,
    status_filter: str | None,
    console: Console,
    *,
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
) -> None:
    """List stored sessions with searchable conversation summaries."""
    try:
        from carbide.governance.session_store import SessionStore

        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        # Determine workspace
        if workspace is None:
            workspace = Path.cwd()

        store_path = workspace / ".carbide" / "sessions.db"
        if not store_path.exists():
            console.print("[yellow]No sessions found. Database does not exist.[/yellow]")
            return

        # Open session store
        store = SessionStore(store_path)
        store.open()

        try:
            # Fetch one extra row so the CLI can tell the user whether another
            # page exists without adding a separate COUNT query.
            page = store.list_sessions(
                limit=limit + 1,
                offset=offset,
                status=status_filter,
                search=search,
            )
            has_next_page = len(page) > limit
            sessions = page[:limit]

            if not sessions:
                console.print("[yellow]No sessions found on this page.[/yellow]")
                return

            heading = f"Sessions in {workspace}"
            if search and search.strip():
                heading += f" matching {search.strip()!r}"
            console.print(f"\n[bold]{heading}:[/bold]\n")

            table = Table(show_header=True, header_style="bold cyan", expand=False)
            table.add_column("ID", no_wrap=True)
            table.add_column("Status", no_wrap=True)
            table.add_column("Summary", min_width=30, max_width=72, no_wrap=True)
            table.add_column("Updated", no_wrap=True)

            for session in sessions:
                session_id = session["id"]
                status = session["status"]
                updated = str(session["updated_at"])[:19].replace("T", " ")
                # Color status
                if status == "completed":
                    status_text = Text(str(status), style="green")
                elif status == "failed":
                    status_text = Text(str(status), style="red")
                else:
                    status_text = Text(str(status), style="yellow")

                table.add_row(
                    Text(str(session_id), style="dim"),
                    status_text,
                    Text(_session_summary(session.get("title"))),
                    updated,
                )

            console.print(table)
            first = offset + 1
            last = offset + len(sessions)
            console.print(f"\nShowing {first}-{last} session(s).")

            def page_command(page_offset: int) -> str:
                command = f"carbide list-sessions --limit {limit} --offset {page_offset}"
                if status_filter:
                    command += f" --status {status_filter}"
                if search and search.strip():
                    command += f" --search {search.strip()!r}"
                return command

            if offset > 0:
                previous = max(0, offset - limit)
                console.print(f"Previous page: {page_command(previous)}")
            if has_next_page:
                console.print(f"Next page: {page_command(offset + limit)}")

        finally:
            store.close()

    except ImportError:
        console.print("[red]Session store not available.[/red]")
    except Exception as e:
        console.print(f"[red]Error listing sessions: {e}[/red]")


def _job_summary(job: BackgroundJob, *, limit: int = 80) -> str:
    """Render a safe single-line command preview for the jobs table."""
    text = " ".join(job.command.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _render_jobs(console: Console, jobs: list[BackgroundJob], workspace: Path) -> None:
    if not jobs:
        console.print(f"[yellow]No managed jobs found in {workspace}.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold cyan", expand=False)
    table.add_column("Job ID", no_wrap=True)
    table.add_column("PID", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Mode", no_wrap=True)
    table.add_column("Session", max_width=14, no_wrap=True)
    for job in jobs:
        status_style = "green" if job.status == "running" else "dim"
        table.add_row(
            job.job_id,
            str(job.pid),
            Text(job.status, style=status_style),
            "detached" if job.detached else "attached",
            _session_summary(job.owner_session_id, limit=14),
        )
    console.print(f"\n[bold]Managed jobs in {workspace}:[/bold]\n")
    console.print(table)
    for job in jobs:
        console.print(f"  {job.job_id}  {_job_summary(job)}", style="dim")


async def jobs_command(
    workspace: Path | None,
    action: str,
    job_id: str | None,
    session_id: str | None,
    include_detached: bool,
    older_than: float,
    console: Console,
) -> int:
    """Inspect or stop persisted background jobs with ownership checks."""
    root = (workspace or Path.cwd()).resolve()
    repository = JobRepository(root / ".carbide" / "jobs")
    manager = BackgroundJobManager(root, session_id or "cli", repository=repository)

    if action == "status":
        jobs = await manager.list_all()
        if session_id:
            jobs = [job for job in jobs if job.owner_session_id == session_id]
        _render_jobs(console, jobs, root)
        return 0

    if not session_id:
        console.print(
            "[red]Stopping a job requires --session so Carbide cannot stop another agent's process.[/red]"
        )
        return 2

    if action == "stop":
        if not job_id:
            console.print("[red]jobs stop requires a job ID.[/red]")
            return 2
        try:
            job = await manager.stop(job_id)
        except (JobNotFoundError, JobOwnershipError) as exc:
            console.print(f"[red]{exc}[/red]")
            return 2
        console.print(f"[green]Stopped job {job.job_id} (pid {job.pid}).[/green]")
        return 0

    if action == "cleanup":
        try:
            stopped = await manager.cleanup_stale(
                older_than,
                include_detached=include_detached,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return 2
        console.print(
            f"[green]Cleaned up {len(stopped)} stale job(s) older than {older_than:g}s.[/green]"
        )
        return 0

    stopped = await manager.stop_all(include_detached=include_detached)
    suffix = " including detached jobs" if include_detached else ""
    console.print(f"[green]Stopped {len(stopped)} job(s){suffix}.[/green]")
    return 0


async def backup_sessions(
    workspace: Path | None, output: Path, overwrite: bool, console: Console
) -> int:
    """Create an atomic backup of a workspace's session database."""
    from carbide.governance.session_store import SessionStore

    root = (workspace or Path.cwd()).resolve()
    store_path = root / ".carbide" / "sessions.db"
    if not store_path.exists():
        console.print(f"[red]Session database does not exist: {store_path}[/red]")
        return 1

    destination = output
    store = SessionStore(store_path)
    try:
        store.open()
        store.backup(destination, overwrite=overwrite)
    except Exception as exc:
        console.print(f"[red]Error backing up sessions: {exc}[/red]")
        return 1
    finally:
        store.close()
    console.print(f"[green]Session backup written to {destination}[/green]")
    return 0


async def check_sessions(workspace: Path | None, console: Console) -> int:
    """Run the SQLite integrity check used by deployment health probes."""
    from carbide.governance.session_store import SessionStore

    root = (workspace or Path.cwd()).resolve()
    store_path = root / ".carbide" / "sessions.db"
    if not store_path.exists():
        console.print(f"[yellow]Session database does not exist: {store_path}[/yellow]")
        return 1

    store = SessionStore(store_path)
    try:
        store.open()
        result = store.integrity_check()
    except Exception as exc:
        console.print(f"[red]Error checking sessions: {exc}[/red]")
        return 1
    finally:
        store.close()
    if result == "ok":
        console.print("[green]Session database integrity: ok[/green]")
        return 0
    console.print(f"[red]Session database integrity: {result}[/red]")
    return 1


async def migrate_sessions(workspace: Path | None, console: Console) -> int:
    """Open the session store and apply/report compatible schema migrations."""
    from carbide.governance.session_store import SessionStore

    root = (workspace or Path.cwd()).resolve()
    store_path = root / ".carbide" / "sessions.db"
    if not store_path.exists():
        console.print(f"[yellow]Session database does not exist: {store_path}[/yellow]")
        return 1

    store = SessionStore(store_path)
    try:
        store.open()
        version = store.schema_version()
    except Exception as exc:
        console.print(f"[red]Error migrating sessions: {exc}[/red]")
        return 1
    finally:
        store.close()
    console.print(f"[green]Session database schema: v{version}[/green]")
    return 0


async def restore_sessions(
    workspace: Path | None, input_path: Path, overwrite: bool, console: Console
) -> int:
    """Restore a validated backup into the workspace session database."""
    from carbide.governance.session_store import SessionStore

    root = (workspace or Path.cwd()).resolve()
    destination = root / ".carbide" / "sessions.db"
    try:
        SessionStore.restore_backup(input_path, destination, overwrite=overwrite)
    except Exception as exc:
        console.print(f"[red]Error restoring sessions: {exc}[/red]")
        return 1
    console.print(f"[green]Session database restored to {destination}[/green]")
    return 0


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    # Handle list-sessions command
    if hasattr(args, "command") and args.command == "list-sessions":
        await list_sessions(
            args.workspace,
            args.status if hasattr(args, "status") else None,
            console,
            limit=args.limit,
            offset=args.offset,
            search=args.search,
        )
        return 0
    if hasattr(args, "command") and args.command == "jobs":
        return await jobs_command(
            args.workspace,
            args.action,
            args.job_id,
            args.session,
            args.include_detached,
            args.older_than,
            console,
        )
    if hasattr(args, "command") and args.command == "backup-sessions":
        return await backup_sessions(args.workspace, args.output, args.overwrite, console)
    if hasattr(args, "command") and args.command == "check-sessions":
        return await check_sessions(args.workspace, console)
    if hasattr(args, "command") and args.command == "migrate-sessions":
        return await migrate_sessions(args.workspace, console)
    if hasattr(args, "command") and args.command == "restore-sessions":
        return await restore_sessions(args.workspace, args.input, args.overwrite, console)

    overrides: dict[str, object] = {}
    if args.max_turns is not None:
        overrides["max_turns"] = args.max_turns

    try:
        config = Config.load(workspace=args.workspace, **overrides)
    except SystemExit as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    if args.no_context_pyramid:
        config.context.enabled = False

    # Create the registry first. Agent initialization adds the governed
    # load_skill capability, after which schemas are sent to the provider.
    registry = create_default_registry()

    renderer = Renderer(console, verbose=args.verbose)

    # Handle --resume
    resume_session_id = args.resume if args.resume else None

    async def approve_tool(call: StreamToolUse, prompt: str) -> bool:
        """Ask for approval without blocking the event loop."""
        answer = await asyncio.to_thread(
            input,
            _approval_prompt(call, prompt),
        )
        return answer.strip().lower() in {"y", "yes"}

    async with Agent(
        config=config,
        tool_registry=registry,
        resume_session_id=resume_session_id,
        memory_write_mode=args.memory_write_mode,
        approval_callback=approve_tool,
    ) as agent:
        tools = registry.api_schemas()
        if args.prompt:
            await run_once(agent, renderer, " ".join(args.prompt), tools=tools)
            for update in await agent.flush_memory():
                _render_memory_result(renderer, update)
        else:
            await run_interactive(agent, renderer, console, tools=tools)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130
