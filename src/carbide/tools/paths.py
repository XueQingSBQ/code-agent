"""Workspace containment, in one place.

Every tool that takes a path routes it through here. Having three tools each
implement their own check is how one of them ends up with a subtly different
rule, and the weakest one is the one that matters.

The rule: resolve the path, then prove the result is inside the workspace. Doing
it in that order is what catches symlinks and ``..`` segments, since both only
show themselves after resolution.
"""

from __future__ import annotations

from pathlib import Path

#: Directories never worth searching. Skipping them is the difference between a
#: grep that returns in 50ms and one that walks a 40k-file node_modules tree.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        ".next",
        ".carbide",
    }
)

ESCAPE_MESSAGE = "Path escapes the workspace: {path}"


def resolve_in_workspace(workspace: Path, raw: str) -> Path | None:
    """Resolve ``raw`` inside ``workspace``, or None if it escapes.

    Absolute paths are allowed as long as they land inside. Returning None
    rather than raising keeps the caller's error message in the caller's voice —
    it is being written for the model to read.
    """
    root = workspace.resolve()
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def is_probably_binary(chunk: bytes) -> bool:
    """A NUL byte in the first block is the same heuristic grep itself uses."""
    return b"\x00" in chunk


def relative_posix(workspace: Path, target: Path) -> str:
    """Workspace-relative, forward slashes, on every platform.

    Output shape is part of the tool's contract. If it varies by platform, so do
    the model's paths, and it starts guessing at separators.
    """
    try:
        rel = target.resolve().relative_to(workspace.resolve())
    except ValueError:
        return target.as_posix()
    return rel.as_posix()
