"""Regression tests for agent-owned background process lifecycle."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from carbide.process import (
    BackgroundJobManager,
    JobOwnershipError,
    extract_background_pid,
    extract_log_path,
)
from carbide.tools.base import ToolContext
from carbide.tools.bash_session import find_bash
from carbide.tools.shell import BashParams, BashTool


def _has_native_bash() -> bool:
    path = find_bash().casefold().replace("\\", "/")
    return path not in {"bash", "bash.exe"} and not path.endswith("/windows/system32/bash.exe")


def _sleep_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_pid_and_log_contract_is_explicit(tmp_path: Path) -> None:
    command = "python -m http.server 8000 > .carbide/server.log 2>&1 & echo $!"
    assert extract_background_pid(command, "12345\n") == 12345
    assert extract_background_pid(command, "server started\n") is None
    assert extract_background_pid("echo 'a&b'", "12345\n") is None
    assert extract_log_path(command, tmp_path) == (tmp_path / ".carbide/server.log").resolve()


@pytest.mark.asyncio
async def test_stop_reclaims_owned_process_and_persists_state(tmp_path: Path) -> None:
    process = _sleep_process()
    manager = BackgroundJobManager(tmp_path, "session-a")
    try:
        job = manager.register(process.pid, "sleeping test process")
        assert job.status == "running"
        stopped = await manager.stop(job.job_id)

        process.wait(timeout=5)
        assert stopped.status == "stopped"
        assert manager.repository.get(job.job_id).status == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_session_ownership_prevents_cross_agent_stop(tmp_path: Path) -> None:
    process = _sleep_process()
    owner = BackgroundJobManager(tmp_path, "session-a")
    foreign = BackgroundJobManager(tmp_path, "session-b")
    try:
        job = owner.register(process.pid, "sleeping test process")
        with pytest.raises(JobOwnershipError):
            await foreign.stop(job.job_id)
        assert process.poll() is None
    finally:
        await owner.stop_all(include_detached=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_close_stops_attached_but_preserves_detached(tmp_path: Path) -> None:
    attached = _sleep_process()
    detached = _sleep_process()
    manager = BackgroundJobManager(tmp_path, "session-a")
    try:
        manager.register(attached.pid, "attached")
        manager.register(detached.pid, "detached", detached=True)

        await manager.close()
        attached.wait(timeout=5)
        assert detached.poll() is None

        await manager.stop_all(include_detached=True)
        detached.wait(timeout=5)
    finally:
        for process in (attached, detached):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.asyncio
async def test_cleanup_stale_is_owner_scoped_and_ttl_based(tmp_path: Path) -> None:
    process = _sleep_process()
    manager = BackgroundJobManager(tmp_path, "session-a")
    try:
        job = manager.register(process.pid, "stale attached process")
        job.started_at = datetime.now(UTC) - timedelta(hours=2)
        manager.repository.save(job)

        stopped = await manager.cleanup_stale(60)
        assert [item.job_id for item in stopped] == [job.job_id]
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(not _has_native_bash(), reason="native Bash is not available")
@pytest.mark.asyncio
async def test_bash_registers_a_real_background_pid(tmp_path: Path) -> None:
    manager = BackgroundJobManager(tmp_path, "session-a")
    tool = BashTool()
    context = ToolContext(
        workspace=tmp_path,
        shell="bash",
        session_id="session-a",
        background_job_manager=manager,
    )
    try:
        result = await tool.run(
            BashParams(
                command=(
                    'python -c "import time; time.sleep(60)" > .carbide/job.log 2>&1 & echo $!'
                )
            ),
            context,
        )
        assert not result.is_error
        assert "[background job]" in result.content
        jobs = await manager.list()
        assert len(jobs) == 1
        assert jobs[0].status == "running", result.content
    finally:
        await manager.stop_all(include_detached=True)
        await BashTool.close_sessions()
