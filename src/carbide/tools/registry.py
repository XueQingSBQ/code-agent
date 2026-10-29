"""Tool pool assembly with a second contract check.

``Tool.__init_subclass__`` already validated the class at import time. The
registry re-checks on registration because tools can also arrive from plugins
or be constructed dynamically, and a wrong ``concurrency_safe`` flag is the kind
of bug that only shows up as corrupted files under load.
"""

from __future__ import annotations

from typing import Any

import pydantic

from carbide.tools.base import Tool, ToolContext, ToolResult

_REQUIRED = ("read_only", "destructive", "concurrency_safe")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    @property
    def tools(self) -> dict[str, Tool]:
        """Compatibility view of registered tools.

        New code should use ``all()``/``get()``. Returning the mapping keeps
        older integrations that inspect ``registry.tools`` working without
        exposing a second mutable registry implementation.
        """
        return self._tools

    def register(self, tool: Tool) -> None:
        self._validate(tool)
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    @staticmethod
    def _validate(tool: Tool) -> None:
        """Validate the safety contract before adding a tool."""
        cls = type(tool)
        for attr in _REQUIRED:
            value = getattr(cls, attr, None)
            if not isinstance(value, bool):
                raise TypeError(f"{cls.__name__}.{attr} must be a bool, got {value!r}")

    def replace(self, tool: Tool) -> None:
        """Replace a registered tool while preserving registry validation.

        Evaluation environments use this to swap the local shell transport
        for a container-backed implementation without rebuilding the default
        file/search tool set.
        """
        self._validate(tool)
        if tool.name not in self._tools:
            raise KeyError(f"cannot replace unknown tool: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def is_concurrency_safe(self, name: str) -> bool:
        """Fail-closed: unknown tools are treated as unsafe."""
        tool = self._tools.get(name)
        if tool is None:
            return False
        return type(tool).concurrency_safe

    def api_schemas(self, *, read_only_only: bool = False) -> list[dict[str, Any]]:
        """Tool definitions for the request. ``read_only_only`` backs plan mode."""
        return [
            type(tool).api_schema()
            for tool in self._tools.values()
            if not read_only_only or type(tool).read_only
        ]

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate arguments, then run. Never raises for ordinary failures."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        try:
            args = type(tool).params.model_validate(arguments)
        except pydantic.ValidationError as exc:
            # Give the model the actual validation errors so it can retry with
            # corrected arguments instead of guessing.
            return ToolResult(
                content=f"Invalid arguments for {name}:\n{exc}",
                is_error=True,
            )
        return await tool.run(args, ctx)
