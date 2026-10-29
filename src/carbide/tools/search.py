"""Search tools.

Grep prefers ripgrep and falls back to a Python walker when it is absent. The
fallback exists because a tool that answers "install ripgrep first" is a dead
end: the model cannot install anything, so it either gives up or starts guessing
file names. Slower and always working beats faster and sometimes broken.

Both backends must produce byte-identical output for the same query, or the
agent's behaviour would depend on which machine it happens to be running on.
That equivalence is asserted in the tests.

Both tools truncate hard. The output cap is not politeness, it is cost control —
every line returned is a line the model pays for on every subsequent turn.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.paths import (
    ESCAPE_MESSAGE,
    SKIP_DIRS,
    is_probably_binary,
    relative_posix,
    resolve_in_workspace,
)

GLOB_LIMIT = 100
GREP_LINE_LIMIT = 200
#: Files larger than this are skipped by the fallback. A 200MB minified bundle
#: has nothing a human wants to read and costs seconds to scan.
MAX_SCAN_BYTES = 5_000_000
SEARCH_TIMEOUT_SECONDS = 30.0
SearchMode = Literal["files_with_matches", "content", "count"]


class _SearchTimeoutError(Exception):
    """Internal signal used to turn a hung search subprocess into a result."""


class GlobParams(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py' or 'src/**/test_*.py'.")


class GlobTool(Tool):
    name: ClassVar[str] = "glob"
    description: ClassVar[str] = (
        "Find files by name pattern. Results are sorted by modification time, newest "
        "first, and capped at 100 files. Narrow the pattern if you need more."
    )
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = GlobParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GlobParams)
        root = ctx.workspace.resolve()
        # ``Path.glob`` is lazy. On Windows, a single reparse point or otherwise
        # inaccessible file can raise from ``is_file`` while the generator is
        # being consumed. A list comprehension lets that one path abort the
        # whole search, which is especially painful for ``**/*`` over a large
        # workspace. Collect candidates one at a time and fail soft per path.
        matches: list[tuple[float, Path]] = []
        skipped = 0
        scan_interrupted = False

        try:
            iterator = root.glob(args.pattern)
        except (NotImplementedError, ValueError) as exc:
            return ToolResult(
                content=f"Invalid glob pattern {args.pattern!r}: {exc}",
                is_error=True,
            )

        while True:
            try:
                path = next(iterator)
            except StopIteration:
                break
            except (NotImplementedError, ValueError) as exc:
                return ToolResult(
                    content=f"Invalid glob pattern {args.pattern!r}: {exc}",
                    is_error=True,
                )
            except (OSError, RuntimeError):
                # The selector can stop while descending into an inaccessible
                # directory. Keep the matches already found and make the
                # incompleteness visible to the model.
                skipped += 1
                scan_interrupted = True
                break

            try:
                # A pattern may contain '..', so containment is checked per
                # result rather than trusted from the pattern.
                if not path.is_file() or resolve_in_workspace(root, str(path)) is None:
                    continue
                matches.append((path.stat().st_mtime, path))
            except (OSError, RuntimeError, ValueError):
                # PermissionError is an OSError. RuntimeError covers symlink
                # loops and ValueError covers malformed path operations.
                skipped += 1

        # Recently touched files are usually the relevant ones. Crude, effective.
        matches.sort(key=lambda item: item[0], reverse=True)
        shown = matches[:GLOB_LIMIT]
        relative_matches: list[str] = []
        for _, path in shown:
            try:
                relative_matches.append(relative_posix(root, path))
            except (OSError, RuntimeError, ValueError):
                skipped += 1

        if relative_matches:
            listing = "\n".join(relative_matches)
        else:
            listing = "No files matched." if not skipped else "No accessible files matched."
        if len(matches) > GLOB_LIMIT:
            listing += f"\n\n[truncated: {len(matches) - GLOB_LIMIT} more matches]"
        if skipped:
            qualifier = "; results may be incomplete" if scan_interrupted else ""
            listing += (
                f"\n\n[warning: skipped {skipped} inaccessible path(s) while scanning{qualifier}]"
            )
        return ToolResult(content=listing or "No files matched.")


class GrepParams(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: str = Field(default=".", description="Directory or file to search under.")
    glob: str | None = Field(default=None, description="Restrict to files matching this glob.")
    mode: SearchMode = Field(
        default="files_with_matches",
        description=(
            "files_with_matches returns paths only (cheapest, default); content returns "
            "matching lines; count returns match counts per file."
        ),
    )
    case_insensitive: bool = False


class GrepTool(Tool):
    name: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Search file contents with a regular expression. Prefer the default "
        "files_with_matches mode and read the interesting files afterwards; use content "
        "mode only when you need the lines themselves. Never invoke grep or rg through "
        "the bash tool — this tool has the correct permission and path handling."
    )
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = GrepParams

    #: Set to False to exercise the fallback on a machine that has ripgrep.
    prefer_ripgrep: ClassVar[bool] = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GrepParams)
        root = ctx.workspace.resolve()
        target = resolve_in_workspace(root, args.path)
        if target is None:
            return ToolResult(content=ESCAPE_MESSAGE.format(path=args.path), is_error=True)
        if not target.exists():
            return ToolResult(content=f"No such path: {args.path}", is_error=True)

        try:
            compiled = re.compile(args.pattern, re.IGNORECASE if args.case_insensitive else 0)
        except re.error as exc:
            # Report before dispatching: the model can fix its own regex, and the
            # message is the same whichever backend would have run.
            return ToolResult(
                content=f"Invalid regular expression {args.pattern!r}: {exc}", is_error=True
            )

        rg = shutil.which("rg") if self.prefer_ripgrep else None
        try:
            if rg is not None:
                lines = await self._ripgrep(rg, args, root, target)
                if lines is None:
                    lines = self._python_search(compiled, args, root, target)
            else:
                lines = self._python_search(compiled, args, root, target)
        except _SearchTimeoutError:
            return ToolResult(
                content=f"Search timed out after {SEARCH_TIMEOUT_SECONDS:.0f}s.",
                is_error=True,
            )

        if not lines:
            return ToolResult(content="No matches.")
        if len(lines) > GREP_LINE_LIMIT:
            kept = "\n".join(lines[:GREP_LINE_LIMIT])
            return ToolResult(
                content=(
                    f"{kept}\n\n[truncated: {len(lines) - GREP_LINE_LIMIT} more lines; "
                    "narrow the pattern or use files_with_matches mode]"
                )
            )
        return ToolResult(content="\n".join(lines))

    async def _ripgrep(
        self, rg: str, args: GrepParams, root: Path, target: Path
    ) -> list[str] | None:
        """Fast path. Returns None if ripgrep could not answer, so we fall back."""
        flags = {
            "files_with_matches": ["--files-with-matches"],
            "content": ["--line-number", "--no-heading", "--with-filename"],
            "count": ["--count"],
        }[args.mode]
        cmd = [rg, *flags, "--color", "never", "--no-messages"]
        # Keep ripgrep's traversal policy identical to the Python fallback.
        # Without explicit globs ripgrep searches node_modules/.git/.carbide,
        # which leaks irrelevant paths and makes results backend-dependent.
        for skipped_dir in sorted(SKIP_DIRS):
            cmd += ["--glob", f"!{skipped_dir}/**"]
        cmd += ["--max-filesize", f"{MAX_SCAN_BYTES}B"]
        if args.case_insensitive:
            cmd.append("--ignore-case")
        if args.glob:
            cmd += ["--glob", args.glob]
        # Search relative to the workspace so ripgrep prints relative paths.
        rel = relative_posix(root, target) if target != root else "."
        cmd += ["--regexp", args.pattern, rel]

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=root,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SEARCH_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            if proc is not None and proc.returncode is None:
                proc.kill()
            if proc is not None:
                await proc.communicate()
            raise _SearchTimeoutError from exc
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
            if proc is not None:
                await proc.communicate()
            raise
        except OSError:
            return None
        # rg exits 1 for "no matches", which is a normal outcome, not an error.
        if proc.returncode not in (0, 1):
            return None
        return [self._normalise(line) for line in stdout.decode(errors="replace").splitlines()]

    @staticmethod
    def _normalise(line: str) -> str:
        """Make ripgrep's paths match the fallback's, on every platform."""
        line = line.replace("\\", "/")
        return line[2:] if line.startswith("./") else line

    def _python_search(
        self, compiled: re.Pattern[str], args: GrepParams, root: Path, target: Path
    ) -> list[str]:
        results: list[str] = []
        for path in self._candidates(root, target, args.glob):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if is_probably_binary(raw[:1024]):
                continue
            text = raw.decode("utf-8", errors="replace")
            rel = relative_posix(root, path)

            if args.mode == "files_with_matches":
                if compiled.search(text):
                    results.append(rel)
                continue

            hits = [
                (number, line)
                for number, line in enumerate(text.splitlines(), start=1)
                if compiled.search(line)
            ]
            if not hits:
                continue
            if args.mode == "count":
                results.append(f"{rel}:{len(hits)}")
            else:
                results.extend(f"{rel}:{number}:{line}" for number, line in hits)
        return results

    @staticmethod
    def _candidates(root: Path, target: Path, glob: str | None) -> list[Path]:
        if target.is_file():
            try:
                if target.stat().st_size > MAX_SCAN_BYTES:
                    return []
            except OSError:
                return []
            return [target]
        found: list[Path] = []
        # rglob would descend into node_modules before we could stop it, so the
        # walk is manual and prunes as it goes.
        stack = [target]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in SKIP_DIRS and not entry.is_symlink():
                        stack.append(entry)
                elif entry.is_file():
                    try:
                        if entry.stat().st_size > MAX_SCAN_BYTES:
                            continue
                    except OSError:
                        continue
                    if glob and not entry.match(glob):
                        continue
                    found.append(entry)
        # Sorted so the output is stable run to run, which keeps the prompt cache
        # from being invalidated by an arbitrary directory ordering.
        found.sort(key=lambda p: relative_posix(root, p))
        return found
