"""Filesystem tools.

No caching, no index, no preprocessing: every call stats and reads from disk.
A file edited a moment ago is visible immediately, which removes a whole class
of "the agent is looking at stale content" bugs.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.paths import ESCAPE_MESSAGE, resolve_in_workspace

DEFAULT_READ_LIMIT = 2000


def _resolve(workspace: Path, raw: str) -> Path | None:
    return resolve_in_workspace(workspace, raw)


class ReadParams(BaseModel):
    path: str = Field(description="File path, absolute or relative to the workspace.")
    offset: int = Field(default=0, ge=0, description="0-indexed first line to read.")
    limit: int = Field(
        default=DEFAULT_READ_LIMIT, ge=1, description="Maximum number of lines to return."
    )


class ReadTool(Tool):
    name: ClassVar[str] = "read"
    description: ClassVar[str] = (
        "Read a file from the workspace. Returns up to 2000 lines by default, with line "
        "numbers. When you already know which part of the file you need, pass offset and "
        "limit and read only that part."
    )
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = ReadParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ReadParams)
        target = _resolve(ctx.workspace, args.path)
        if target is None:
            return ToolResult(content=ESCAPE_MESSAGE.format(path=args.path), is_error=True)
        if not target.is_file():
            return ToolResult(content=f"Not a file: {args.path}", is_error=True)
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(content=f"Could not read {args.path}: {exc}", is_error=True)

        window = lines[args.offset : args.offset + args.limit]
        numbered = "\n".join(f"{args.offset + i + 1:>6}\t{line}" for i, line in enumerate(window))
        remaining = len(lines) - (args.offset + len(window))
        if remaining > 0:
            numbered += f"\n\n[truncated: {remaining} more lines; use offset to continue]"
        return ToolResult(content=numbered or "[empty file]")


class WriteParams(BaseModel):
    path: str = Field(description="File path to write.")
    content: str = Field(description="Full file contents.")


class WriteTool(Tool):
    name: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "Write a complete file, creating parent directories as needed. Overwrites any "
        "existing content, so read the file first unless you are creating it."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = True
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = WriteParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WriteParams)
        target = _resolve(ctx.workspace, args.path)
        if target is None:
            return ToolResult(content=ESCAPE_MESSAGE.format(path=args.path), is_error=True)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(content=f"Could not write {args.path}: {exc}", is_error=True)
        line_count = args.content.count("\n") + 1
        return ToolResult(content=f"Wrote {args.path} ({line_count} lines).")


class EditParams(BaseModel):
    path: str = Field(description="File path to edit.")
    old_text: str = Field(description="Exact text to replace. Must appear exactly once.")
    new_text: str = Field(description="Replacement text.")


class EditTool(Tool):
    name: ClassVar[str] = "edit"
    description: ClassVar[str] = (
        "Replace an exact string in a file. old_text must match exactly once, including "
        "whitespace; include surrounding context to make it unique."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = True
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.WRITE
    params: ClassVar[type[BaseModel]] = EditParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, EditParams)
        target = _resolve(ctx.workspace, args.path)
        if target is None:
            return ToolResult(content=ESCAPE_MESSAGE.format(path=args.path), is_error=True)
        if not target.is_file():
            return ToolResult(content=f"Not a file: {args.path}", is_error=True)
        try:
            original = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(content=f"Could not read {args.path}: {exc}", is_error=True)

        occurrences = original.count(args.old_text)
        if occurrences == 0:
            return ToolResult(
                content=f"old_text not found in {args.path}. Read the file and retry.",
                is_error=True,
            )
        if occurrences > 1:
            return ToolResult(
                content=(
                    f"old_text appears {occurrences} times in {args.path}. "
                    "Add surrounding context to make it unique."
                ),
                is_error=True,
            )
        try:
            target.write_text(original.replace(args.old_text, args.new_text, 1), encoding="utf-8")
        except OSError as exc:
            return ToolResult(content=f"Could not write {args.path}: {exc}", is_error=True)
        return ToolResult(content=f"Edited {args.path}.")
