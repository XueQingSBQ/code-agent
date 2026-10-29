"""Safe Git worktree lifecycle for generic Agent Workers.

The manager uses argument-list Git subprocesses and never invokes a shell. A
Worker modifies only its detached worktree; integration is serialized and
rebases onto a newer clean parent HEAD when possible.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class WorktreeError(RuntimeError):
    """A worktree precondition or Git lifecycle operation failed."""


@dataclass(frozen=True, slots=True)
class WorktreeHandle:
    """A prepared detached worktree and the parent snapshot it came from."""

    repository: Path
    path: Path
    workspace: Path
    base_commit: str
    run_id: str


@dataclass(frozen=True, slots=True)
class WorktreeOutcome:
    """The result of committing and attempting to merge one child worktree."""

    status: str
    commit: str | None = None
    error: str | None = None
    worktree_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class WorktreeManager:
    """Create, finalize, and clean up isolated Git worktrees."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.expanduser().resolve()
        self._merge_lock = asyncio.Lock()

    async def prepare(self, run_id: str) -> WorktreeHandle:
        """Validate a clean parent and create a detached child worktree."""
        repository = await self._repository_root()
        await self._require_clean(repository)
        base_commit = await self._git_text(repository, "rev-parse", "HEAD")

        relative_workspace = self.project_dir.relative_to(repository)
        worktree_path = repository.parent / f".carbide-worktree-{run_id}"
        if worktree_path.exists():
            raise WorktreeError(f"Worktree path already exists: {worktree_path}")

        result = await self._git(repository, "worktree", "add", "--detach", worktree_path, base_commit)
        if result.returncode != 0:
            with contextlib.suppress(Exception):
                await self._git(repository, "worktree", "remove", "--force", worktree_path)
            raise WorktreeError(_git_failure("create worktree", result))

        workspace = worktree_path / relative_workspace
        if not workspace.is_dir():
            await self.cleanup(
                WorktreeHandle(repository, worktree_path, workspace, base_commit, run_id)
            )
            raise WorktreeError(f"Worktree workspace does not exist: {workspace}")
        return WorktreeHandle(
            repository=repository,
            path=worktree_path,
            workspace=workspace,
            base_commit=base_commit,
            run_id=run_id,
        )

    async def finalize(
        self,
        handle: WorktreeHandle,
        *,
        commit_message: str,
    ) -> WorktreeOutcome:
        """Commit child changes and merge them into the unchanged parent.

        Conflicts are always aborted.  The parent is never left in a partial
        merge state, and no automatic conflict resolution is attempted.
        """
        changes = await self._git(handle.path, "status", "--porcelain=v1", "--untracked-files=all")
        if changes.returncode != 0:
            return WorktreeOutcome("failed", error=_git_failure("inspect worktree", changes))
        if not _has_meaningful_changes(changes.stdout):
            await self.cleanup(handle)
            return WorktreeOutcome("no_changes", metadata={"base_commit": handle.base_commit})

        staged = await self._git(handle.path, "add", "--all", ".", ":!.carbide")
        if staged.returncode != 0:
            return WorktreeOutcome("failed", error=_git_failure("stage worktree changes", staged))
        committed = await self._git(handle.path, "commit", "--no-verify", "-m", commit_message)
        if committed.returncode != 0:
            return WorktreeOutcome("failed", error=_git_failure("commit worktree changes", committed))
        commit = await self._git_text(handle.path, "rev-parse", "HEAD")

        async with self._merge_lock:
            parent_head = await self._git_text(handle.repository, "rev-parse", "HEAD")
            parent_status = await self._git(
                handle.repository,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            if parent_status.returncode != 0 or _has_meaningful_changes(parent_status.stdout):
                await self.cleanup(handle)
                return WorktreeOutcome(
                    "merge_blocked",
                    commit=commit,
                    error=(
                        "Parent repository has uncommitted changes while Worker "
                        "was running; automatic merge was refused."
                    ),
                    metadata={"base_commit": handle.base_commit},
                )

            if parent_head != handle.base_commit:
                ancestor = await self._git(
                    handle.repository,
                    "merge-base",
                    "--is-ancestor",
                    handle.base_commit,
                    parent_head,
                )
                if ancestor.returncode != 0:
                    await self.cleanup(handle)
                    return WorktreeOutcome(
                        "merge_blocked",
                        commit=commit,
                        error=(
                            "Parent repository moved to an unrelated history; "
                            "automatic rebase was refused."
                        ),
                        metadata={"base_commit": handle.base_commit},
                    )
                rebased = await self._git(handle.path, "rebase", parent_head)
                if rebased.returncode != 0:
                    with contextlib.suppress(Exception):
                        await self._git(handle.path, "rebase", "--abort")
                    await self.cleanup(handle)
                    return WorktreeOutcome(
                        "merge_blocked",
                        commit=commit,
                        error=_git_failure("rebase child commit onto the current parent", rebased),
                        metadata={"base_commit": handle.base_commit},
                    )
                commit = await self._git_text(handle.path, "rev-parse", "HEAD")

            merged = await self._git(
                handle.repository,
                "merge",
                "--no-ff",
                "--no-edit",
                commit,
            )
            if merged.returncode != 0:
                await self._git(handle.repository, "merge", "--abort")
                await self.cleanup(handle)
                return WorktreeOutcome(
                    "merge_blocked",
                    commit=commit,
                    error=_git_failure("merge child commit", merged),
                    metadata={"base_commit": handle.base_commit},
                )

        await self.cleanup(handle)
        return WorktreeOutcome(
            "merged",
            commit=commit,
            metadata={"base_commit": handle.base_commit},
        )

    async def cleanup(self, handle: WorktreeHandle) -> None:
        """Remove exactly one registered worktree and prune stale metadata."""
        result = await self._git(handle.repository, "worktree", "remove", "--force", handle.path)
        if result.returncode != 0 and handle.path.exists():
            raise WorktreeError(_git_failure("remove worktree", result))
        await self._git(handle.repository, "worktree", "prune")

    async def _repository_root(self) -> Path:
        result = await self._git(self.project_dir, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise WorktreeError(_git_failure("find Git repository", result))
        return await asyncio.to_thread(Path(result.stdout.strip()).resolve)

    async def _require_clean(self, repository: Path) -> None:
        result = await self._git(repository, "status", "--porcelain=v1", "--untracked-files=all")
        if result.returncode != 0:
            raise WorktreeError(_git_failure("inspect parent repository", result))
        if _has_meaningful_changes(result.stdout):
            raise WorktreeError(
                "Worker requires a clean parent repository; uncommitted changes were found."
            )

    @staticmethod
    async def _git(cwd: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
        command = ["git", *[str(argument) for argument in arguments]]

        def run() -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(
                    command,
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=120,
                )
            except OSError as exc:
                return subprocess.CompletedProcess(command, 1, "", str(exc))
            except subprocess.TimeoutExpired as exc:
                return subprocess.CompletedProcess(command, 1, "", f"Git timed out: {exc}")

        return await asyncio.to_thread(run)

    @classmethod
    async def _git_text(cls, cwd: Path, *arguments: object) -> str:
        result = await cls._git(cwd, *arguments)
        if result.returncode != 0:
            raise WorktreeError(_git_failure("run Git command", result))
        value = result.stdout.strip()
        if not value:
            raise WorktreeError("Git command returned no output")
        return value


def _git_failure(operation: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "unknown Git error").strip()
    return f"Could not {operation}: {detail}"


def _has_meaningful_changes(status: str) -> bool:
    """Ignore only untracked Carbide runtime files in porcelain output."""
    for line in status.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? ") and _is_runtime_path(line[3:]):
            continue
        return True
    return False


def _is_runtime_path(raw_path: str) -> bool:
    """Return whether a porcelain path belongs to the agent runtime directory."""
    path = raw_path.strip().replace("\\", "/").strip('"')
    return path == ".carbide" or path.startswith(".carbide/")
