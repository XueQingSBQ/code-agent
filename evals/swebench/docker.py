"""
Docker container management for SWE-bench evaluation.

Provides isolated execution environments for running evaluations.
Each instance gets its own container with the correct Python version
and repository state.
"""

import asyncio
import contextlib
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.bash_session import BashSession
from carbide.tools.shell import BashParams

from .instance import SWEBenchInstance


@dataclass(frozen=True)
class DockerResourceLimits:
    """Resource ceilings applied to every evaluation container."""

    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512

    def __post_init__(self) -> None:
        if not math.isfinite(self.cpus) or self.cpus <= 0:
            raise ValueError("Docker CPU limit must be a finite positive number")
        if not re.fullmatch(r"[1-9]\d*(?:\.\d+)?[bBkKmMgGtT]?", self.memory):
            raise ValueError("Docker memory limit must be a positive value such as '512m' or '4g'")
        if self.pids_limit < 1:
            raise ValueError("Docker PID limit must be at least 1")


class DockerEnvironment:
    """Manages Docker containers for SWE-bench evaluation."""

    def __init__(
        self,
        instance: SWEBenchInstance,
        work_dir: Path,
        resource_limits: DockerResourceLimits | None = None,
    ):
        self.instance = instance
        self.work_dir = work_dir
        self.resource_limits = resource_limits or DockerResourceLimits()
        self.container_id: str | None = None
        self._shell_sessions: list[BashSession] = []
        safe_instance_id = instance.instance_id.replace("/", "_").replace("\\", "_")
        self.container_name = f"swebench_{safe_instance_id}_{uuid.uuid4().hex[:8]}"

    async def setup(self) -> None:
        """
        Create and configure a Docker container for this instance.

        Sets up:
        - Python environment matching the repo requirements
        - Git checkout at the correct commit
        - Test dependencies installed
        """
        # Extract repo owner and name
        repo_parts = self.instance.repo.split("/")
        if len(repo_parts) != 2 or not all(repo_parts):
            raise ValueError(f"Invalid repo format: {self.instance.repo}")

        # Create container with appropriate Python version
        await asyncio.to_thread(self.work_dir.mkdir, parents=True, exist_ok=True)
        cmd = self._build_run_command()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create container (exit {proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )

        self.container_id = stdout.decode().strip()

        # Clone repository
        await self._exec(["git", "clone", f"https://github.com/{self.instance.repo}.git", "repo"])

        # Checkout correct commit
        await self._exec(["git", "checkout", self.instance.base_commit], cwd="/workspace/repo")

    def _build_run_command(self) -> list[str]:
        """Build the Docker command with explicit resource ceilings."""
        return [
            "docker",
            "run",
            "-d",
            "--name",
            self.container_name,
            "--cpus",
            str(self.resource_limits.cpus),
            "--memory",
            self.resource_limits.memory,
            "--pids-limit",
            str(self.resource_limits.pids_limit),
            "-v",
            f"{self.work_dir.absolute()}:/workspace",
            "-w",
            "/workspace",
            f"python:{self.instance.version}",
            "sleep",
            "infinity",
        ]

    async def _exec(
        self, cmd: list[str], cwd: str | None = None, *, check: bool = True
    ) -> tuple[str, str]:
        """Execute a command inside the container, optionally checking status."""
        stdout, stderr, returncode = await self._exec_status(cmd, cwd)
        if check and returncode != 0:
            rendered = " ".join(cmd)
            raise RuntimeError(
                f"Container command failed (exit {returncode}): {rendered}\n{stderr.strip()}"
            )
        return stdout, stderr

    async def _exec_status(self, cmd: list[str], cwd: str | None = None) -> tuple[str, str, int]:
        """Execute a command and return stdout, stderr, and exit status."""
        if not self.container_id:
            raise RuntimeError("Container not initialized")

        full_cmd = ["docker", "exec"]
        if cwd:
            full_cmd.extend(["-w", cwd])
        full_cmd.append(self.container_id)
        full_cmd.extend(cmd)

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        return (
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            proc.returncode or 0,
        )

    def create_shell_session(self) -> BashSession:
        """Create a persistent Bash transport backed by ``docker exec``."""
        if not self.container_id:
            raise RuntimeError("Container not initialized")
        session = BashSession(
            workspace="/workspace/repo",
            shell_backend="container",
            command=(
                "docker",
                "exec",
                "-i",
                "-w",
                "/workspace/repo",
                self.container_id,
                "bash",
                "-l",
            ),
            process_cwd=None,
        )
        self._shell_sessions.append(session)
        return session

    async def _close_shell_sessions(self) -> None:
        """Close host-side docker exec transports before removing the container."""
        sessions = self._shell_sessions
        self._shell_sessions = []
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    async def apply_test_patch(self) -> None:
        """Apply the test patch to enable validation."""
        if not self.container_id:
            raise RuntimeError("Container not initialized")

        if not self.instance.test_patch.strip():
            return

        # Write patch to file
        patch_file = self.work_dir / "test.patch"
        await asyncio.to_thread(
            patch_file.write_text,
            self.instance.test_patch,
            encoding="utf-8",
        )

        # Apply patch
        await self._exec(["git", "apply", "/workspace/test.patch"], cwd="/workspace/repo")

    async def run_tests(self) -> dict[str, Any]:
        """
        Run the test suite and return results.

        Returns:
            Dict with keys: success (bool), passed_tests (list), failed_tests (list)
        """
        if not self.container_id:
            raise RuntimeError("Container not initialized")

        # Run each test independently.  ``pytest -x`` over a combined list
        # stops at the first failure and cannot tell which later tests passed.
        all_tests = list(dict.fromkeys(self.instance.FAIL_TO_PASS + self.instance.PASS_TO_PASS))

        if not all_tests:
            return {"success": True, "passed_tests": [], "failed_tests": []}

        passed = []
        failed = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        for test in all_tests:
            stdout, stderr, returncode = await self._exec_status(
                ["python", "-m", "pytest", "-q", test],
                cwd="/workspace/repo",
            )
            stdout_parts.append(stdout)
            stderr_parts.append(stderr)
            if returncode == 0:
                passed.append(test)
            else:
                failed.append(test)

        success = len(failed) == 0

        return {
            "success": success,
            "passed_tests": passed,
            "failed_tests": failed,
            "stdout": "\n".join(stdout_parts),
            "stderr": "\n".join(stderr_parts),
        }

    async def cleanup(self) -> None:
        """Stop and remove the container."""
        await self._close_shell_sessions()
        if self.container_id:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    self.container_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
            finally:
                self.container_id = None


class DockerBashTool(Tool):
    """Run the agent's Bash calls inside its SWE-bench container.

    File and search tools continue to use the host path mounted at
    ``/workspace``; arbitrary process execution is routed through a persistent
    ``docker exec`` transport so commands cannot escape the evaluation
    container.
    """

    name = "bash"
    description = (
        "Run a shell command inside the isolated SWE-bench evaluation container. "
        "Use the dedicated read, write, edit, glob and grep tools for file work."
    )
    read_only = False
    destructive = True
    concurrency_safe = False
    risk_level = RiskLevel.DESTRUCTIVE
    params = BashParams

    def __init__(self, environment: DockerEnvironment) -> None:
        self._session = environment.create_shell_session()

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        if not isinstance(args, BashParams):
            return ToolResult(content="Invalid arguments for bash", is_error=True)
        try:
            output, exit_code = await self._session.execute(args.command, timeout=args.timeout)
            body = output.strip() or "[no output]"
            if exit_code != 0:
                return ToolResult(content=f"exit {exit_code}\n{body}", is_error=True)
            return ToolResult(content=body)
        except TimeoutError:
            return ToolResult(
                content=f"Command timed out after {args.timeout}s and was killed.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(content=f"Shell execution failed: {exc}", is_error=True)
