"""Persistent bash session for multi-turn command execution.

Unlike ShellTool which spawns a new process per command, BashSession maintains
a single long-lived bash process that preserves state (cwd, env vars, shell
functions) across commands.

Key design decisions:
- Echo unique markers to reliably split output from multiple commands
- Exit code captured via `echo $?` after each command
- Async reader task to prevent deadlock on large output
- Timeout per command, not per session (timeout doesn't kill the process)
- Windows: explicitly uses Git Bash to avoid WSL bash wrapper
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

# Unique markers to separate command output.
# Using UUID prefix makes collision with real output virtually impossible.
_MARKER_PREFIX = f"__CARBIDE_{uuid.uuid4().hex[:8]}__"
_START_MARKER = f"{_MARKER_PREFIX}START"
_END_MARKER = f"{_MARKER_PREFIX}END"
_EXIT_CODE_MARKER = f"{_MARKER_PREFIX}EXIT"


def _find_bash() -> str:
    """Find the bash executable, preferring Git Bash on Windows.

    On Windows, multiple bash.exe may exist (Git Bash, WSL, w64devkit).
    We must use Git Bash explicitly because WSL bash outputs UTF-16LE
    and requires different handling.
    """
    if platform.system() == "Windows":
        # Try Git Bash first (most common and reliable)
        git_bash = r"C:\Program Files\Git\usr\bin\bash.exe"
        if Path(git_bash).exists():
            return git_bash
        # Fallback to w64devkit or other MSYS2-based bash
        candidates = [
            r"E:\w64devkit\bin\bash.exe",
            r"C:\msys64\usr\bin\bash.exe",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return candidate
        # Last resort: use shutil.which but warn if it might be WSL
        bash = shutil.which("bash")
        if bash and "System32" not in bash and "WindowsApps" not in bash:
            return bash
        raise RuntimeError(
            "Could not find Git Bash. Install Git for Windows: https://git-scm.com/download/win"
        )
    else:
        # On Unix, just use bash from PATH
        bash = shutil.which("bash")
        if bash:
            return bash
        raise RuntimeError("bash not found in PATH")


class BashSession:
    """A persistent bash process for multi-turn interaction.

    Usage:
        async with BashSession(workspace=Path.cwd()) as session:
            output, code = await session.execute("echo hello")
            output, code = await session.execute("pwd")
    """

    def __init__(self, workspace: Path, *, timeout: float = 120.0) -> None:
        self._workspace = workspace.resolve()
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stdout_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()  # Serialize command execution

    async def __aenter__(self) -> BashSession:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the event loop that owns the subprocess transport."""
        return self._loop

    async def start(self) -> None:
        """Spawn the bash process and configure it."""
        current_loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current_loop:
            self.terminate()
        if self._proc is not None:
            if self._proc.returncode is None and self._proc.stdin is not None:
                return
            await self.close()

        bash_exe = _find_bash()

        # Spawn bash in non-interactive mode with UTF-8 encoding
        env = {
            **os.environ,
            "TERM": "dumb",  # Disable colors/control codes
            "LC_ALL": "C.UTF-8",  # Force UTF-8 locale
            "LANG": "C.UTF-8",
        }
        self._proc = await asyncio.create_subprocess_exec(
            bash_exe,
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout
            cwd=self._workspace,
            env=env,
        )
        self._loop = current_loop

        # Start background reader
        self._reader_task = asyncio.create_task(self._read_loop())

        # Give bash a moment to start
        await asyncio.sleep(0.05)

        # Test the session with a simple echo
        test_cmd = f'echo "{_START_MARKER}"\n'
        assert self._proc.stdin is not None
        self._proc.stdin.write(test_cmd.encode("utf-8"))
        await self._proc.stdin.drain()

        try:
            await self._drain_until_marker(_START_MARKER, timeout_seconds=2.0)
            log.debug("bash_session_started", workspace=str(self._workspace))
        except (TimeoutError, RuntimeError) as exc:
            # Check if process died
            returncode = self._proc.returncode if self._proc is not None else None
            await self.close()
            if returncode is not None:
                raise RuntimeError(f"Bash process died with code {returncode}") from exc
            raise RuntimeError("Failed to initialize bash session") from exc

    async def execute(self, command: str) -> tuple[str, int]:
        """Execute a command in the session.

        Returns:
            (output, exit_code) where output includes both stdout and stderr.

        Raises:
            TimeoutError: if the command doesn't complete within the timeout.
            RuntimeError: if the shell cannot be started or has died.
        """
        async with self._lock:  # Only one command at a time
            # A previous timeout, Ctrl+C, or shell-level exit can invalidate
            # the transport.  Recreate it transparently for the next tool
            # call; callers should not have to restart the whole Agent.
            if self._proc is None or self._proc.returncode is not None:
                await self.start()

            # Wrap command: start marker, command, exit code capture, end marker
            # Key: ensure markers appear on their own lines to avoid concatenation
            wrapped = (
                f'printf "%s\\n" "{_START_MARKER}"\n'
                f"{command}\n"
                f"__exit_code=$?\n"
                f'printf "\\n%s\\n" "{_EXIT_CODE_MARKER}$__exit_code"\n'
                f'printf "%s\\n" "{_END_MARKER}"\n'
            )
            try:
                await self._send_raw(wrapped)
            except (OSError, RuntimeError) as exc:
                await self.close()
                raise RuntimeError("Failed to send command to bash session") from exc

            # Collect output until we see the end marker
            try:
                output = await self._drain_until_marker(
                    _END_MARKER,
                    timeout_seconds=self._timeout,
                )
            except (TimeoutError, RuntimeError):
                # A timed-out command may leave a child process or partial
                # protocol output behind.  Discard the whole transport so the
                # next invocation starts from a known state.
                await self.close()
                raise

            # Parse: everything between START and EXIT_CODE is the actual output
            actual_output, exit_code = self._parse_output(output)

            log.debug(
                "bash_command_executed",
                command=command[:100],
                exit_code=exit_code,
                output_len=len(actual_output),
            )
            return actual_output, exit_code

    async def close(self) -> None:
        """Gracefully shut down the session."""
        current_loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current_loop:
            self.terminate()
            return
        if self._proc is None:
            if self._reader_task is not None:
                self._reader_task.cancel()
                await asyncio.gather(self._reader_task, return_exceptions=True)
                self._reader_task = None
            return

        process = self._proc
        # Send exit command
        try:
            if process.returncode is None:
                await self._send_raw("exit\n")
                await asyncio.wait_for(process.wait(), timeout=2.0)
        except (TimeoutError, ProcessLookupError, OSError):
            # Force kill if graceful exit failed
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass

        # Cancel reader task
        reader_task = self._reader_task
        if reader_task is not None:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)

        self._proc = None
        self._loop = None
        self._reader_task = None
        while not self._stdout_queue.empty():
            self._stdout_queue.get_nowait()

        log.debug("bash_session_closed")

    def terminate(self) -> None:
        """Synchronously tear down a session owned by another event loop."""
        process = self._proc
        self._proc = None
        self._loop = None
        self._reader_task = None
        if process is None:
            return
        with contextlib.suppress(Exception):
            if process.returncode is None:
                process.kill()
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        with contextlib.suppress(Exception):
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

    async def _send_raw(self, text: str) -> None:
        """Write text to stdin."""
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(text.encode() + b"\n")
        await self._proc.stdin.drain()

    async def _drain_until_marker(self, marker: str, *, timeout_seconds: float) -> str:
        """Collect lines from the queue until we see the specified marker."""
        lines: list[str] = []
        deadline = asyncio.get_event_loop().time() + timeout_seconds

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Command timed out after {timeout_seconds}s")

            try:
                line = await asyncio.wait_for(self._stdout_queue.get(), timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError(f"Command timed out after {timeout_seconds}s") from exc

            if line is None:
                raise RuntimeError("Bash process terminated unexpectedly")
            lines.append(line)
            # Check if marker appears in this line
            if marker in line:
                break

        return "".join(lines)

    async def _read_loop(self) -> None:
        """Background task: read stdout line by line and push to queue."""
        assert self._proc is not None and self._proc.stdout is not None

        try:
            while True:
                line_bytes = await self._proc.stdout.readline()
                if not line_bytes:  # EOF
                    break
                # Git Bash outputs UTF-8, so we can decode directly
                line = line_bytes.decode("utf-8", errors="replace")
                await self._stdout_queue.put(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("bash_reader_error", error=str(exc))
        finally:
            await self._stdout_queue.put(None)

    def _parse_output(self, raw: str) -> tuple[str, int]:
        """Extract actual output and exit code from the wrapped response."""
        lines = raw.splitlines(keepends=True)

        # Find markers
        start_idx = None
        exit_idx = None
        for i, line in enumerate(lines):
            if _START_MARKER in line:
                start_idx = i
            elif _EXIT_CODE_MARKER in line:
                exit_idx = i

        # Extract exit code
        exit_code = -1
        if exit_idx is not None:
            exit_line = lines[exit_idx]
            try:
                code_str = exit_line.split(_EXIT_CODE_MARKER)[1].strip()
                exit_code = int(code_str)
            except (IndexError, ValueError):
                pass

        # Extract actual output (between START and EXIT_CODE markers)
        if start_idx is not None and exit_idx is not None:
            output_lines = lines[start_idx + 1 : exit_idx]
            return "".join(output_lines), exit_code

        # Fallback: return everything
        return raw, exit_code
