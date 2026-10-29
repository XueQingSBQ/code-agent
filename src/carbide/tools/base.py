"""Tool contract, enforced at import time.

TypeScript can force a tool to declare its safety properties at compile time.
Python cannot, so the contract is enforced by four independent defences:

1. ``__init_subclass__`` raises at import time if a property is missing
2. the registry asserts again when assembling the tool pool
3. mypy strict catches type mismatches
4. ``tests/test_tool_contract.py`` walks every registered tool

Defence 1 is the important one: a tool that forgets to declare whether it is
safe to run concurrently fails the process on import, not in production.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, get_type_hints

from pydantic import BaseModel

from carbide.governance.permission import RiskLevel

_REQUIRED: tuple[str, ...] = (
    "name",
    "description",
    "read_only",
    "destructive",
    "concurrency_safe",
    "risk_level",
)


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    # Full content when the visible content is a truncated preview.
    spill_path: str | None = None


class ToolContext(BaseModel):
    """What a tool is allowed to know about the world."""

    model_config = {"arbitrary_types_allowed": True}

    workspace: Any
    shell: str = "gitbash"
    session_id: str | None = None
    # A child Worker can keep its own conversation session while operating on
    # the parent session's shared Todo plan.
    todo_session_id: str | None = None
    # The runtime identity used by Todo claim/lease checks.  This is injected
    # by the Worker runtime and is never model-supplied.
    todo_owner_id: str | None = None
    # Optional composition-root service for registering and reclaiming
    # processes started by the shell tool. ``Any`` keeps tools independent of
    # the process implementation and preserves the lightweight tool contract.
    background_job_manager: Any | None = None


class Tool(ABC):
    """Base class for every tool.

    Subclasses must declare all six class variables. ``params`` is a Pydantic
    model, which serves as the single source of truth: it generates the JSON
    schema sent to the API *and* validates whatever the model sends back.
    """

    name: ClassVar[str]
    description: ClassVar[str]

    #: May run in plan mode.
    read_only: ClassVar[bool]
    #: Needs user confirmation before running.
    destructive: ClassVar[bool]
    #: Safe to run in parallel with other tools. Fail-closed: when in doubt, False.
    concurrency_safe: ClassVar[bool]
    #: Risk level for permission engine (SAFE, WRITE, or DESTRUCTIVE).
    risk_level: ClassVar[RiskLevel]

    params: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return  # still abstract, nothing to enforce yet
        missing = [attr for attr in _REQUIRED if not hasattr(cls, attr)]
        if missing:
            raise TypeError(
                f"{cls.__name__} does not declare required tool properties: {', '.join(missing)}. "
                "Every tool must state whether it is read-only, destructive and concurrency-safe."
            )
        if not hasattr(cls, "params"):
            raise TypeError(f"{cls.__name__} must define a `params` Pydantic model.")
        hints = get_type_hints(cls)
        for flag in ("read_only", "destructive", "concurrency_safe"):
            value = getattr(cls, flag)
            if not isinstance(value, bool):
                raise TypeError(
                    f"{cls.__name__}.{flag} must be a bool, got {type(value).__name__}. "
                    f"(declared as {hints.get(flag)})"
                )

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Execute the tool. ``args`` is already validated against ``params``."""

    @classmethod
    def api_schema(cls) -> dict[str, Any]:
        """The tool definition sent to the model."""
        schema = cls.params.model_json_schema()
        schema.pop("title", None)
        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": schema,
        }
