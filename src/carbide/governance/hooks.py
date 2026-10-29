"""Hook system for tool execution interception.

Hooks allow intercepting tool calls before and after execution. Use cases:
- Logging and audit trails
- Custom permission checks beyond the standard engine
- Command rewriting (e.g., safety wrappers)
- Metrics and telemetry

Hooks are synchronous by design: they must return immediately or the execution
pipeline stalls. Async hooks would require awaiting at every call site, which
adds latency even when no hooks are registered.

Hook events:
- PRE_TOOL: Before tool execution (can block)
- POST_TOOL: After successful tool execution
- ON_ERROR: When tool raises an exception
- PRE_API_CALL: Before calling the provider API
- POST_API_CALL: After API response received
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from carbide.tools.base import Tool, ToolResult


class HookEvent(StrEnum):
    """Hook event types."""

    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    ON_ERROR = "on_error"
    PRE_API_CALL = "pre_api_call"
    POST_API_CALL = "post_api_call"


@dataclass
class HookContext:
    """Context passed to hooks."""

    tool_name: str
    arguments: BaseModel
    workspace: str


class BeforeHook(Protocol):
    """Hook called before tool execution.

    Return None to allow execution, or a ToolResult to short-circuit.
    """

    def __call__(self, tool: Tool, ctx: HookContext) -> ToolResult | None: ...


class AfterHook(Protocol):
    """Hook called after tool execution.

    Can inspect or modify the result. Return value replaces the original result.
    """

    def __call__(self, tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult: ...


class ErrorHook(Protocol):
    """Hook called when tool execution raises an exception."""

    def __call__(self, tool: Tool, ctx: HookContext, error: Exception) -> None: ...


class APIHook(Protocol):
    """Hook called before/after API calls."""

    def __call__(self, messages: list[dict[str, Any]]) -> None: ...


class HookRegistry:
    """Registry for execution hooks."""

    def __init__(self, hooks_dir: Path | None = None) -> None:
        # Keep the directory as metadata only.  Loading arbitrary Python from a
        # project directory would be an implicit code-execution boundary.  A
        # future hook loader must therefore be explicit and separately
        # sandboxed; the registry itself remains safe to construct when the
        # optional directory is present.
        self.hooks_dir = hooks_dir
        self._scanned = False
        self._before: list[BeforeHook] = []
        self._after: list[AfterHook] = []
        self._error: list[ErrorHook] = []
        self._pre_api: list[APIHook] = []
        self._post_api: list[APIHook] = []

    def scan(self) -> int:
        """Validate the optional hook directory and return loaded hook count.

        Hook discovery is intentionally conservative: project files are not
        imported as code.  The method exists as a stable lifecycle boundary so
        SDK startup cannot fail because an old caller expects ``scan()``.
        Hooks can still be registered programmatically with the typed methods.
        """
        if self.hooks_dir is not None and self.hooks_dir.exists() and not self.hooks_dir.is_dir():
            raise NotADirectoryError(f"Hook path is not a directory: {self.hooks_dir}")
        self._scanned = True
        return 0

    def register_before(self, hook: BeforeHook) -> None:
        """Register a pre-execution hook."""
        self._before.append(hook)

    def register_after(self, hook: AfterHook) -> None:
        """Register a post-execution hook."""
        self._after.append(hook)

    def register_error(self, hook: ErrorHook) -> None:
        """Register an error hook."""
        self._error.append(hook)

    def register_pre_api(self, hook: APIHook) -> None:
        """Register a pre-API hook."""
        self._pre_api.append(hook)

    def register_post_api(self, hook: APIHook) -> None:
        """Register a post-API hook."""
        self._post_api.append(hook)

    def clear(self) -> None:
        """Clear all registered hooks."""
        self._before.clear()
        self._after.clear()
        self._error.clear()
        self._pre_api.clear()
        self._post_api.clear()

    def run_before(self, tool: Tool, ctx: HookContext) -> ToolResult | None:
        """Run all before hooks. Returns short-circuit result if any hook provides one."""
        for hook in self._before:
            result = hook(tool, ctx)
            if result is not None:
                return result
        return None

    def run_after(self, tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult:
        """Run all after hooks, chaining results."""
        current = result
        for hook in self._after:
            current = hook(tool, ctx, current)
        return current

    def run_error(self, tool: Tool, ctx: HookContext, error: Exception) -> None:
        """Run all error hooks."""
        for hook in self._error:
            with contextlib.suppress(Exception):
                hook(tool, ctx, error)

    def run_pre_api(self, messages: list[dict[str, Any]]) -> None:
        """Run all pre-API hooks."""
        for hook in self._pre_api:
            hook(messages)

    def run_post_api(self, messages: list[dict[str, Any]]) -> None:
        """Run all post-API hooks."""
        for hook in self._post_api:
            hook(messages)
