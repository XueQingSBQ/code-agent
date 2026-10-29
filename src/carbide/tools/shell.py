"""Shell tool.

The backend is selectable because Windows has no single answer: git-bash (MSYS2)
is what a developer already has, WSL is what Linux-shaped tooling needs. The
choice matters beyond convenience — it decides which shell's quoting rules the
AST safety analyser in M2 has to model, so it is configuration, not a guess.

M0 runs commands directly. Permission gating and AST analysis are wired in at M2;
until then this tool is destructive and not concurrency-safe, which is what keeps
the executor from running it in parallel with anything else.

M1 adds persistent BashSession to maintain state (cwd, env vars, functions) across
multiple tool invocations.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from typing import ClassVar

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.bash_session import BashSession, find_bash

OUTPUT_LIMIT = 30_000
DEFAULT_TIMEOUT = 120.0

_MARKDOWN_LINK = re.compile(r"\[[^\]\r\n]+\]\([^\)\r\n]+\)")
_LONG_RUNNING_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+http\.server\b", re.IGNORECASE),
        "Python's http.server",
    ),
    (
        re.compile(
            r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b",
            re.IGNORECASE,
        ),
        "a JavaScript development server or watcher",
    ),
    (
        re.compile(
            r"\b(?:vite|uvicorn|gunicorn|flask\s+run|next\s+dev|php\s+-S)\b",
            re.IGNORECASE,
        ),
        "a development server",
    ),
    (
        re.compile(
            r"\bnode(?:\.exe)?\b[^\r\n]*(?:createServer|\.listen\s*\(|\blisten\s*\()",
            re.IGNORECASE,
        ),
        "a Node.js server",
    ),
)
_PID_CAPTURE = re.compile(r"\$!")


def _has_unquoted_background_operator(command: str) -> bool:
    """Return whether *command* contains a standalone, unquoted ``&``.

    This intentionally handles only the small piece of shell syntax needed by
    the lifecycle policy. It does not attempt to parse a shell program, and it
    ignores ampersands inside quoted URLs/JavaScript and the ``&&`` operator.
    """

    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "&":
            previous = command[index - 1] if index else ""
            following = command[index + 1] if index + 1 < len(command) else ""
            if previous != "&" and following != "&":
                return True
    return False


def validate_command_lifecycle(command: str) -> str | None:
    """Validate command forms that would otherwise strand the agent loop.

    The persistent Bash protocol waits for a completion marker. A foreground
    development server never reaches that marker, so it must be backgrounded
    explicitly. Returning a structured tool error lets the model retry without
    spawning an orphan process or waiting for the global command timeout.
    """

    markdown_match = _MARKDOWN_LINK.search(command)
    if markdown_match:
        return (
            "Command contains Markdown link syntax, which is not valid shell syntax: "
            f"{markdown_match.group(0)!r}. Use the plain URL, for example "
            "http://127.0.0.1:8000/."
        )

    has_background = _has_unquoted_background_operator(command)
    if has_background:
        # A long-running child without an explicit PID cannot be safely
        # reclaimed when the agent exits. Requiring ``echo $!`` turns process
        # ownership into an observable contract instead of a best effort.
        for pattern, description in _LONG_RUNNING_COMMANDS:
            if pattern.search(command) and _PID_CAPTURE.search(command) is None:
                return (
                    f"Refusing to start {description} without a tracked PID. "
                    "Background it and print the child PID with `echo $!`, for example: "
                    "python -m http.server 8000 --bind 127.0.0.1 "
                    "> .carbide/server.log 2>&1 & echo $!"
                )
        return None

    for pattern, description in _LONG_RUNNING_COMMANDS:
        if pattern.search(command):
            return (
                f"Refusing to run {description} in the foreground: the Bash tool waits "
                "for the command to exit. Start it in the background with output "
                "redirected to a file inside the workspace, then verify it with a "
                "separate short-lived command. Example: "
                "python -m http.server 8000 --bind 127.0.0.1 "
                "> .carbide/server.log 2>&1 & echo $!"
            )
    return None


def _shell_command(backend: str, command: str) -> list[str]:
    """Build the argv that runs ``command`` under the chosen backend."""
    if backend == "wsl":
        wsl = shutil.which("wsl") or "wsl"
        return [wsl, "bash", "-lc", command]
    if backend == "gitbash":
        # On Windows, ``bash`` on PATH may be the WSL launcher in
        # System32. Use the same executable selection as the persistent
        # session so both execution paths behave consistently.
        bash = find_bash()
        return [bash, "-lc", command]
    return ["/bin/bash", "-lc", command]


class BashParams(BaseModel):
    command: str = Field(
        description=(
            "Shell command to run. Long-running servers and watchers must be started "
            "in the background with output redirected to a workspace file."
        )
    )
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=600, description="Timeout in seconds.")
    detached: bool = Field(
        default=False,
        description=(
            "Keep a tracked background process alive after this agent exits. "
            "Use only when the user explicitly wants a persistent service."
        ),
    )


class BashTool(Tool):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = (
        "Run a shell command in the workspace. Use the dedicated read, write, edit, glob "
        "and grep tools for file work instead of cat/sed/find/grep — they have correct "
        "permission handling. Reserve this for builds, tests and version control. "
        "Never run a long-lived server or watcher in the foreground; background it "
        "and redirect its logs inside the workspace."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = True
    # Never parallel: two shells writing the same tree is how you corrupt a repo.
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.DESTRUCTIVE
    params: ClassVar[type[BaseModel]] = BashParams

    # Class-level session cache: one persistent session per (workspace, shell,
    # event-loop) tuple. asyncio subprocess transports belong to the loop that
    # created them, so separate live loops must not share a session.
    _sessions: ClassVar[dict[tuple[str, str, asyncio.AbstractEventLoop], BashSession]] = {}

    @classmethod
    async def close_sessions(cls) -> None:
        """Close every cached session and clear the process cache.

        Bash sessions are cached by event loop because asyncio subprocess
        transports cannot be shared between loops.  Callers that own the
        tool lifetime (an ``Agent``, test harness, or short-lived CLI loop)
        should use this method during shutdown so a closed loop never leaves
        a child process behind.
        """
        sessions = list(cls._sessions.values())
        cls._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    def _get_session(self, ctx: ToolContext) -> BashSession:
        """Get or create a persistent bash session for this workspace."""
        loop = asyncio.get_running_loop()

        # Pytest and applications can close an event loop while the class-level
        # cache still holds a BashSession. Kill those transports synchronously
        # before they become leaked subprocesses or are reused by a new loop.
        for stale_key, stale_session in list(self._sessions.items()):
            stale_loop = stale_session.loop
            if stale_loop is not None and stale_loop.is_closed():
                stale_session.terminate()
                del self._sessions[stale_key]

        # Keep the loop object itself in the key. Using ``id(loop)`` can reuse
        # an address after pytest closes one loop, accidentally binding a new
        # call to the old loop's subprocess transport.
        key = (str(ctx.workspace), ctx.shell, loop)
        if key not in self._sessions:
            self._sessions[key] = BashSession(ctx.workspace, ctx.shell)
        return self._sessions[key]

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BashParams)

        lifecycle_error = validate_command_lifecycle(args.command)
        if lifecycle_error is not None:
            return ToolResult(content=lifecycle_error, is_error=True)

        # Use persistent session to maintain state across commands
        session = self._get_session(ctx)

        try:
            output, exit_code = await session.execute(args.command, timeout=args.timeout)
            body = output.strip() or "[no output]"

            if exit_code == 0 and ctx.background_job_manager is not None:
                try:
                    job = ctx.background_job_manager.register_from_command(
                        args.command,
                        output,
                        detached=args.detached,
                        backend=ctx.shell,
                    )
                except Exception as exc:
                    # The command already ran. Returning an ordinary warning
                    # prevents a model retry from starting a duplicate server.
                    body += f"\n[background job tracking warning] {type(exc).__name__}: {exc}"
                else:
                    if job is not None:
                        mode = "detached" if job.detached else "attached"
                        body += (
                            f"\n[background job] id={job.job_id} pid={job.pid} "
                            f"status={job.status} mode={mode}"
                        )

            if exit_code != 0:
                return ToolResult(content=f"exit {exit_code}\n{body}", is_error=True)
            return ToolResult(content=body)

        except TimeoutError:
            return ToolResult(
                content=f"Command timed out after {args.timeout}s and was killed.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                content=f"Shell execution failed: {exc}",
                is_error=True,
            )
