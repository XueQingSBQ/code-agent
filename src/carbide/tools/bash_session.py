"""Persistent bash session for stateful shell interactions.

Maintains a long-running bash process to preserve state (cwd, env vars, functions)
across multiple tool invocations. This is the M1 core deliverable.
"""

# ``execute(..., timeout=...)`` is a stable public API used by integrations.
# Keep the parameter name even though ASYNC109 prefers ``timeout_seconds``.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

OUTPUT_LIMIT = 30_000
COMMAND_TIMEOUT = 120.0

_WINDOWS_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Git\bin\bash.exe",
)
_SESSION_TERMINATOR = re.compile(r"(?m)(?:^|[;&|]\s*)(?:exit|exec)\b")


def find_bash() -> str:
    """Find a real Bash executable, avoiding Windows' WSL launcher.

    ``shutil.which('bash')`` commonly resolves to
    ``C:\\Windows\\System32\\bash.exe`` on Windows. That executable is the
    legacy WSL launcher, not Git Bash; starting it as a persistent pipe can
    leave the session waiting forever when WSL is unavailable. Prefer Git
    Bash explicitly for the ``gitbash`` backend, then use a non-WSL Bash on
    PATH as a fallback.
    """
    platform_name = str(getattr(sys, "platform", ""))
    if platform_name.startswith("win"):
        for candidate in _WINDOWS_GIT_BASH_CANDIDATES:
            if Path(candidate).is_file():
                return candidate

        on_path = shutil.which("bash")
        if on_path:
            normalized = str(Path(on_path).resolve()).casefold().replace("\\", "/")
            if not normalized.endswith("/windows/system32/bash.exe") and not normalized.endswith(
                "/windowsapps/bash.exe"
            ):
                return on_path

        # Preserve the historical fallback so the caller can report the
        # subprocess error if no compatible Bash is installed.
        return "bash"

    bash = shutil.which("bash")
    return bash if bash is not None else "/bin/bash"


class BashSession:
    """A persistent bash session that maintains state across commands."""

    def __init__(
        self,
        workspace: str,
        shell_backend: str = "bash",
        *,
        command: Sequence[str] | None = None,
        process_cwd: str | None = None,
    ):
        self.workspace = workspace
        self.shell_backend = shell_backend
        self._command = list(command) if command is not None else None
        self._process_cwd = (
            process_cwd if process_cwd is not None else (workspace if command is None else None)
        )
        self._process: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._delimiter = f"__CARBIDE_END_{uuid.uuid4().hex}__"
        self._lock = asyncio.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Event loop owning the subprocess transport, if it has started."""
        return self._loop

    def terminate(self) -> None:
        """Stop a stale subprocess without awaiting its old event loop."""
        process = self._process
        self._process = None
        self._loop = None
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()
        self._close_transport(process)

    @staticmethod
    def _close_transport(process: asyncio.subprocess.Process | None) -> None:
        """Close subprocess pipes after termination, including on Windows."""
        if process is None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        with contextlib.suppress(Exception):
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

    async def start(self) -> None:
        """Start the persistent bash process."""
        current_loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current_loop:
            self.terminate()

        if self._process is not None:
            stdin = self._process.stdin
            if self._process.returncode is None and stdin is not None and not stdin.is_closing():
                return
            self.terminate()

        self._loop = current_loop

        # A caller may provide a transport command, such as ``docker exec``.
        # The same delimiter protocol then preserves shell state regardless of
        # whether Bash runs locally or inside an evaluation container.
        if self._command is not None:
            cmd = self._command
        elif self.shell_backend == "wsl":
            cmd = ["wsl", "bash", "-l"]
        elif self.shell_backend == "gitbash":
            bash_path = self._find_bash()
            cmd = [bash_path, "-l"]
        else:
            # Auto-detect on Windows, otherwise use /bin/bash
            bash_path = self._find_bash()
            cmd = [bash_path, "-l"]

        # Start interactive bash process
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._process_cwd,
            env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"},
            **self._process_group_options(),
        )

        # Wait for shell to be ready
        await asyncio.sleep(0.1)

        # Drain any startup output
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("Bash process did not expose stdout")
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=0.01)
                if not line:
                    break
        except TimeoutError:
            return

    def _find_bash(self) -> str:
        """Find bash executable based on platform."""
        return find_bash()

    async def execute(
        self,
        command: str,
        timeout: float = COMMAND_TIMEOUT,
    ) -> tuple[str, int]:
        """Execute a command and return (output, exit_code).

        Args:
            command: Shell command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (output_text, exit_code)
        """
        async with self._lock:
            if self._process is None or self._process.returncode is not None:
                await self.start()

            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise RuntimeError("Bash process is not ready")

            # Construct command with exit code capture and delimiter
            command_to_run = command
            if _SESSION_TERMINATOR.search(command):
                # Run explicit ``exit``/``exec`` commands in a nested Bash.
                # Git Bash on Windows can propagate ``exit`` through a plain
                # parenthesized subshell and kill the persistent transport;
                # ``bash -c`` preserves the command's status and output while
                # keeping the session usable for the next request. State
                # changes made by a command that explicitly exits are not
                # expected to persist.
                command_to_run = f"( bash -c {shlex.quote(command)} )"

            wrapped_command = (
                # The EXIT trap covers commands such as ``exit 3``. Without
                # it the persistent shell terminates before it can emit the
                # normal marker, so the caller only sees "process terminated"
                # instead of the command's exit status.
                f'trap \'printf "{self._delimiter}%s\\n" "$?"\' EXIT\n'
                f"{command_to_run}\n"
                # A command such as ``printf hello`` may not write a trailing
                # newline. The reader below also recognises a marker appended
                # to a final output line, so the marker cannot be swallowed.
                f"__carbide_status=$?\n"
                f"printf '{self._delimiter}%s\\n' \"$__carbide_status\"\n"
                f"trap - EXIT"
            )

            try:
                # Send command
                process.stdin.write(wrapped_command.encode() + b"\n")
                await process.stdin.drain()

                # Read output until we see the delimiter
                output_lines = []
                exit_code = 0
                start_time = asyncio.get_event_loop().time()

                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise TimeoutError(f"Command timed out after {timeout}s")

                    # A quiet command is not a timed-out command. Poll at
                    # most once per second so the total command deadline can
                    # be checked without confusing a read poll timeout with
                    # the caller's actual timeout.
                    try:
                        line = await asyncio.wait_for(
                            process.stdout.readline(), timeout=min(1.0, remaining)
                        )
                    except TimeoutError:
                        continue

                    if not line:
                        # Process died
                        self._process = None
                        raise RuntimeError("Bash process terminated unexpectedly")

                    decoded = line.decode(errors="replace").rstrip("\n")

                    # Check for delimiter with exit code. It may be the whole
                    # line or be appended directly to output from a command
                    # that did not write a trailing newline.
                    marker_index = decoded.find(self._delimiter)
                    if marker_index >= 0:
                        output_prefix = decoded[:marker_index]
                        if output_prefix:
                            output_lines.append(output_prefix)
                        exit_code_str = decoded[marker_index + len(self._delimiter) :]
                        with contextlib.suppress(ValueError):
                            exit_code = int(exit_code_str)
                        break

                    output_lines.append(decoded)

                # A command may have terminated the shell after the EXIT
                # marker was emitted. Do not reuse its closed stdin on the
                # next invocation; the following call will start a fresh
                # session automatically.
                await asyncio.sleep(0)
                if (
                    process.returncode is not None
                    or process.stdin is None
                    or process.stdin.is_closing()
                ):
                    self._process = None
                    self._close_transport(process)

                output = "\n".join(output_lines)

                # Truncate if too long
                if len(output) > OUTPUT_LIMIT:
                    head = output[: OUTPUT_LIMIT // 2]
                    tail = output[-OUTPUT_LIMIT // 2 :]
                    output = f"{head}\n\n[... {len(output) - OUTPUT_LIMIT} characters elided ...]\n\n{tail}"

                return output, exit_code

            except (TimeoutError, RuntimeError, OSError):
                # Kill and restart the session on timeout
                await self.close()
                raise
            except asyncio.CancelledError:
                # A cancelled tool must not leave a foreground server or its
                # descendants attached to the persistent shell.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.close())
                raise

    @staticmethod
    def _process_group_options() -> dict[str, Any]:
        """Create an isolated process group for reliable tree termination."""

        if os.name == "nt":
            return {
                "creationflags": int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            }
        return {"start_new_session": True}

    async def _kill_process_tree(self, process: asyncio.subprocess.Process) -> None:
        """Terminate the shell and descendants after a timeout or failed close."""

        if process.returncode is not None:
            return

        if os.name == "nt":
            # ``Process.kill`` only targets bash.exe. taskkill /T also removes
            # Python/Node children that would otherwise keep listening on a
            # port after the command timed out.
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=2.0)
        else:
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if callable(killpg) and callable(getpgid):
                    killpg(getpgid(process.pid), sigkill)

        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()

    async def close(self) -> None:
        """Close the bash session."""
        process = self._process
        if process is None:
            return

        try:
            if process.stdin:
                process.stdin.write(b"exit\n")
                await process.stdin.drain()

            await asyncio.wait_for(process.wait(), timeout=2.0)
        except Exception:
            with contextlib.suppress(Exception):
                await self._kill_process_tree(process)
            with contextlib.suppress(Exception):
                await process.wait()
        finally:
            self._close_transport(process)
            self._process = None
            self._loop = None

    async def __aenter__(self) -> BashSession:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
