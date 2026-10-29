"""Lifecycle manager for agent-owned background processes."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from carbide.process.domain import BackgroundJob
from carbide.process.errors import JobOwnershipError
from carbide.process.repository import JobRepository

_PID_LINE = re.compile(r"^\s*(?:\[\d+\]\s+)?(\d+)\s*$")
_PID_CAPTURE = re.compile(r"\$!")
_WINDOWS_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Git\bin\bash.exe",
)
_REDIRECT = re.compile(r"(?:^|\s)(?:2>>?|1>>?|>>?|&>)\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s;&]+))")


def _has_background_operator(command: str) -> bool:
    """Recognise a standalone, unquoted shell background operator."""
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


def extract_background_pid(command: str, output: str) -> int | None:
    """Extract the PID from the explicit ``echo $!`` contract.

    The shell tool only registers a process when the command both backgrounds
    something and prints ``$!``. This avoids guessing from arbitrary numeric
    command output and makes ownership auditable.
    """
    if not _has_background_operator(command) or _PID_CAPTURE.search(command) is None:
        return None
    for line in reversed(output.splitlines()):
        match = _PID_LINE.match(line)
        if match:
            pid = int(match.group(1))
            if pid > 0:
                return pid
    return None


def extract_log_path(command: str, workspace: Path) -> Path | None:
    """Extract a workspace-contained output redirection when one is obvious."""
    for match in _REDIRECT.finditer(command):
        raw = next((value for value in match.groups() if value is not None), "")
        if not raw or raw.startswith("&"):
            continue
        try:
            candidate = (
                (workspace / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            )
            candidate.relative_to(workspace)
        except (OSError, ValueError):
            return None
        return candidate
    return None


def _uses_wsl_backend(backend: str | None) -> bool:
    """Whether a Windows job PID lives inside WSL rather than Win32."""
    if os.name != "nt":
        return False
    if backend == "wsl":
        return True
    if backend not in {"bash", "gitbash"}:
        return False
    if any(Path(candidate).is_file() for candidate in _WINDOWS_GIT_BASH_CANDIDATES):
        return False
    bash = shutil.which("bash")
    if bash is None:
        return False
    normalized = str(Path(bash).resolve()).casefold().replace("\\", "/")
    return normalized.endswith("/windows/system32/bash.exe") or normalized.endswith(
        "/windowsapps/bash.exe"
    )


def _native_bash_path() -> str | None:
    """Resolve Git Bash without accidentally selecting the WSL launcher."""
    for candidate in _WINDOWS_GIT_BASH_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    on_path = shutil.which("bash")
    if on_path is None:
        return None
    normalized = str(Path(on_path).resolve()).casefold().replace("\\", "/")
    if normalized.endswith("/windows/system32/bash.exe") or normalized.endswith(
        "/windowsapps/bash.exe"
    ):
        return None
    return on_path


def _shell_control_argv(backend: str | None, script: str) -> list[str] | None:
    """Build a command that can inspect/terminate a shell-native PID."""
    if os.name != "nt":
        return None
    if backend == "wsl" or _uses_wsl_backend(backend):
        wsl = shutil.which("wsl") or "wsl"
        return [wsl, "bash", "-lc", script]
    if backend in {"bash", "gitbash"}:
        bash = _native_bash_path()
        if bash is not None:
            return [bash, "-lc", script]
    return None


def _is_running_sync(pid: int, backend: str | None = None) -> bool:
    if pid <= 0:
        return False
    control_argv = _shell_control_argv(backend, f"kill -0 {pid} >/dev/null 2>&1")
    if control_argv is not None:
        try:
            result = subprocess.run(
                control_argv,
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and f'"{pid}"' in result.stdout

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _descendants_sync(root_pid: int) -> list[int]:
    """Return descendants without requiring psutil."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)

    found: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        found.append(pid)
        pending.extend(children.get(pid, []))
    return found


def _terminate_sync(pid: int, backend: str | None = None) -> None:
    """Terminate one process tree, using platform-native tree termination."""
    if not _is_running_sync(pid, backend):
        return
    control_argv = _shell_control_argv(
        backend,
        f"kill -TERM {pid} >/dev/null 2>&1 || true; "
        f"sleep 0.15; kill -KILL {pid} >/dev/null 2>&1 || true",
    )
    if control_argv is not None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                control_argv,
                capture_output=True,
                timeout=5.0,
                check=False,
            )
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5.0,
                check=False,
            )
        return

    descendants = _descendants_sync(pid)
    for child in reversed(descendants):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(child, signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)
    time.sleep(0.15)
    for candidate in [*descendants, pid]:
        if _is_running_sync(candidate):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(candidate, getattr(signal, "SIGKILL", signal.SIGTERM))


class BackgroundJobManager:
    """Own, inspect, and stop background jobs created by one agent session."""

    def __init__(
        self,
        workspace: Path,
        owner_session_id: str,
        *,
        shell_backend: str | None = None,
        repository: JobRepository | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.owner_session_id = owner_session_id
        self.shell_backend = shell_backend
        self.repository = repository or JobRepository(self.workspace / ".carbide" / "jobs")

    def register(
        self,
        pid: int,
        command: str,
        *,
        detached: bool = False,
        backend: str | None = None,
        log_path: Path | None = None,
    ) -> BackgroundJob:
        """Persist a process discovered from a successful Bash invocation."""
        now = datetime.now(UTC)
        process_backend = backend or self.shell_backend
        running = _is_running_sync(pid, process_backend)
        job = BackgroundJob(
            job_id=str(uuid.uuid4()),
            pid=pid,
            command=command,
            workspace=self.workspace,
            owner_session_id=self.owner_session_id,
            status="running" if running else "stopped",
            started_at=now,
            detached=detached,
            backend=process_backend,
            log_path=log_path,
            stopped_at=None if running else now,
            last_checked_at=now,
        )
        self.repository.save(job)
        return job

    def register_from_command(
        self,
        command: str,
        output: str,
        *,
        detached: bool = False,
        backend: str | None = None,
    ) -> BackgroundJob | None:
        """Register a job only when the Bash PID contract is present."""
        pid = extract_background_pid(command, output)
        if pid is None:
            return None
        return self.register(
            pid,
            command,
            detached=detached,
            backend=backend or self.shell_backend,
            log_path=extract_log_path(command, self.workspace),
        )

    def _owned(self, job_id: str) -> BackgroundJob:
        job = self.repository.get(job_id)
        if job.owner_session_id != self.owner_session_id:
            raise JobOwnershipError(f"background job {job_id} belongs to another session")
        return job

    def _refresh_sync(self, job: BackgroundJob) -> BackgroundJob:
        now = datetime.now(UTC)
        running = _is_running_sync(job.pid, job.backend)
        if running:
            job.status = "running"
        elif job.status == "running":
            job.status = "stopped"
            job.stopped_at = job.stopped_at or now
        job.last_checked_at = now
        self.repository.save(job)
        return job

    async def status(self, job_id: str) -> BackgroundJob:
        """Refresh and return one owned job."""
        job = self._owned(job_id)
        return await asyncio.to_thread(self._refresh_sync, job)

    async def list(self, *, refresh: bool = True) -> builtins.list[BackgroundJob]:
        """List only jobs owned by this session."""
        jobs = self.repository.list(owner_session_id=self.owner_session_id)
        if not refresh:
            return jobs
        return list(await asyncio.gather(*(self.status(job.job_id) for job in jobs)))

    async def list_all(self, *, refresh: bool = True) -> builtins.list[BackgroundJob]:
        """List all jobs in this workspace for an explicit operator view.

        This is intentionally separate from :meth:`list`: agents never use
        this method, while the CLI needs a read-only workspace-wide view to
        help an operator identify the correct owner session before stopping a
        process.
        """
        jobs = self.repository.list()
        if not refresh:
            return jobs
        return list(
            await asyncio.gather(*(asyncio.to_thread(self._refresh_sync, job) for job in jobs))
        )

    async def stop(self, job_id: str) -> BackgroundJob:
        """Stop one owned job and persist its terminal state."""
        job = self._owned(job_id)
        await asyncio.to_thread(_terminate_sync, job.pid, job.backend)
        job.status = "stopped" if not _is_running_sync(job.pid, job.backend) else "unknown"
        job.stopped_at = datetime.now(UTC)
        job.last_checked_at = job.stopped_at
        self.repository.save(job)
        return job

    async def stop_all(self, *, include_detached: bool = False) -> builtins.list[BackgroundJob]:
        """Stop this session's owned jobs, preserving explicitly detached jobs."""
        jobs = await self.list()
        stopped: list[BackgroundJob] = []
        for job in jobs:
            if job.status == "running" and (include_detached or not job.detached):
                stopped.append(await self.stop(job.job_id))
        return stopped

    async def cleanup_stale(
        self,
        max_age_seconds: float,
        *,
        include_detached: bool = False,
    ) -> builtins.list[BackgroundJob]:
        """Stop old attached jobs owned by this session.

        This is intentionally opt-in and owner-scoped. A stale metadata file
        is not enough evidence to kill a reused PID belonging to another user;
        operators must choose the session and age threshold explicitly.
        """
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        jobs = await self.list()
        stopped: list[BackgroundJob] = []
        for job in jobs:
            if (
                job.status == "running"
                and job.started_at <= cutoff
                and (include_detached or not job.detached)
            ):
                stopped.append(await self.stop(job.job_id))
        return stopped

    async def close(self) -> None:
        """Agent shutdown hook: reclaim attached jobs, leave detached jobs alive."""
        await self.stop_all(include_detached=False)
