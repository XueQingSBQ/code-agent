"""Skills system: three-tier progressive disclosure."""

from carbide.skills.base import Skill, SkillLoader, SkillMetadata, SkillRegistry
from carbide.skills.manager import SkillManager, SkillWriter, create_skill_template
from carbide.skills.paths import agent_skills_dir, carbide_home
from carbide.skills.tool import LoadSkillParams, LoadSkillTool

__all__ = [
    "LoadSkillParams",
    "LoadSkillTool",
    "Skill",
    "SkillLoader",
    "SkillManager",
    "SkillMetadata",
    "SkillRegistry",
    "SkillWriter",
    "agent_skills_dir",
    "carbide_home",
    "create_skill_template",
]
