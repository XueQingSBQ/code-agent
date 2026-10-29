"""Tool for loading an installed Agent skill on model request."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.skills.manager import SkillManager
from carbide.tools.base import Tool, ToolContext, ToolResult


class LoadSkillParams(BaseModel):
    """Arguments accepted by ``load_skill``."""

    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$",
        description="Installed Agent skill name, without the leading slash.",
    )


class LoadSkillTool(Tool):
    """Load full instructions for one globally installed Agent skill."""

    name: ClassVar[str] = "load_skill"
    description: ClassVar[str] = (
        "Load the full instructions for an available Agent skill. Use this when the task "
        "matches a listed skill; pass the skill name without a leading slash."
    )
    read_only: ClassVar[bool] = True
    destructive: ClassVar[bool] = False
    concurrency_safe: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    params: ClassVar[type[BaseModel]] = LoadSkillParams

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        assert isinstance(args, LoadSkillParams)
        skill, error = self._manager.invoke(args.name)
        if skill is None:
            return ToolResult(content=error or f"Skill not found: {args.name}", is_error=True)
        return ToolResult(content=(f"[Skill loaded: {skill.metadata.name}]\n\n{skill.content}"))
