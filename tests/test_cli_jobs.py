"""CLI coverage for managed background-process operations."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

from carbide.cli.app import build_parser, jobs_command
from carbide.process import BackgroundJobManager


def test_jobs_parser_exposes_safe_operations() -> None:
    args = build_parser().parse_args(["jobs", "stop-all", "-C", "workspace", "--session", "s1"])
    assert args.command == "jobs"
    assert args.action == "stop-all"
    assert args.session == "s1"


@pytest.mark.asyncio
async def test_jobs_stop_requires_session(tmp_path: Path) -> None:
    output = io.StringIO()
    code = await jobs_command(
        tmp_path,
        "stop",
        "missing",
        None,
        False,
        86_400.0,
        Console(file=output),
    )
    assert code == 2
    assert "--session" in output.getvalue()


@pytest.mark.asyncio
async def test_jobs_status_lists_job_owner(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output = io.StringIO()
    manager = BackgroundJobManager(tmp_path, "session-a")
    try:
        job = manager.register(process.pid, "test-server")
        code = await jobs_command(
            tmp_path,
            "status",
            None,
            "session-a",
            False,
            86_400.0,
            Console(file=output, width=120),
        )
        assert code == 0
        rendered = output.getvalue()
        assert job.job_id in rendered
        assert "session-a" in rendered
    finally:
        await manager.stop_all(include_detached=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
