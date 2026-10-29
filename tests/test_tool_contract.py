"""Defence 4 of the tool contract: walk every registered tool.

Defence 1 (``__init_subclass__``) already fires at import time. This test exists
because a tool can also be registered dynamically, and because a *wrong*
``concurrency_safe`` value is not a type error — it is a data-corruption bug that
only shows up under load.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from carbide.sdk import default_registry
from carbide.tools.base import Tool, ToolContext, ToolResult


def test_every_tool_declares_safety_properties() -> None:
    for tool in default_registry().all():
        cls = type(tool)
        for attr in ("read_only", "destructive", "concurrency_safe"):
            assert isinstance(getattr(cls, attr), bool), f"{cls.__name__}.{attr}"
        assert cls.name and cls.description
        assert issubclass(cls.params, BaseModel)


def test_mutating_tools_are_never_concurrency_safe() -> None:
    """Writers must not run in parallel. Two shells or two writers touching the
    same tree is how a repo gets corrupted."""
    for tool in default_registry().all():
        cls = type(tool)
        if not cls.read_only:
            assert not cls.concurrency_safe, f"{cls.name} mutates but claims to be parallel-safe"


def test_read_only_tools_are_not_destructive() -> None:
    for tool in default_registry().all():
        cls = type(tool)
        if cls.read_only:
            assert not cls.destructive, f"{cls.name} is read-only but marked destructive"


def test_missing_declaration_fails_at_class_creation() -> None:
    from carbide.governance.permission import RiskLevel

    class Params(BaseModel):
        x: int = 0

    with pytest.raises(TypeError, match="risk_level"):

        class Incomplete(Tool):
            name = "incomplete"
            description = "missing a safety declaration"
            read_only = True
            destructive = False
            params = Params

            async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
                return ToolResult(content="")


def test_non_bool_declaration_is_rejected() -> None:
    from carbide.governance.permission import RiskLevel

    class Params(BaseModel):
        x: int = 0

    with pytest.raises(TypeError, match="must be a bool"):

        class Sloppy(Tool):
            name = "sloppy"
            description = "truthy is not true"
            read_only = 1  # type: ignore[assignment]
            destructive = False
            concurrency_safe = False
            risk_level = RiskLevel.SAFE
            params = Params

            async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
                return ToolResult(content="")


def test_unknown_tool_is_treated_as_unsafe() -> None:
    """Fail-closed: an unregistered name must never be run in parallel."""
    registry = default_registry()
    assert registry.is_concurrency_safe("does_not_exist") is False


def test_registry_rejects_duplicate_names() -> None:
    from carbide.tools.fs import ReadTool

    registry = default_registry()
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(ReadTool())
