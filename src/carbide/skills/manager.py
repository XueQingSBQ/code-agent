"""Skill manager: handles skill invocation and caching.

Manages active skills and ensures they're injected as user messages
to preserve prompt cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from carbide.skills.base import Skill, SkillLoader, SkillRegistry


class SkillManager:
    """
    Manages skill lifecycle: discovery, loading, caching, injection.

    Key features:
    - Lazy loading (only load when invoked)
    - Active skill tracking (for restoration after L4 compaction)
    - Cache-friendly injection (as user messages, not system prompt)
    """

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.registry = SkillRegistry(skills_dir)
        self.loader = SkillLoader(skills_dir)

        # Track active skills (for L4.5 restoration)
        self.active_skills: dict[str, Skill] = {}

    def scan(self) -> None:
        """Scan and register all skills."""
        self.registry.scan()

    def get_skill_list(self) -> str:
        """Get Tier 1 skill list (minimal context)."""
        return self.registry.format_skill_list()

    def activate_from_prompt(self, user_message: str) -> str | None:
        """Activate a globally installed skill addressed as ``/skill-name``.

        Ordinary project paths and slash-containing prose are ignored. Only a
        registered skill name in the first whitespace-delimited token can
        activate a skill.
        """

        first_token = user_message.strip().split(maxsplit=1)[0] if user_message.strip() else ""
        if not first_token.startswith("/"):
            return None

        name = first_token[1:]
        if not name or name not in self.registry.skills:
            return None

        skill, error = self.invoke(name)
        return skill.metadata.name if skill is not None and not error else None

    def invoke(self, name: str) -> tuple[Skill | None, str]:
        """
        Invoke a skill by name.

        Returns:
            (skill, error_message)
        """
        # Check if already active (cached)
        if name in self.active_skills:
            return self.active_skills[name], ""

        # Load skill
        skill = self.loader.load(name)
        if not skill:
            return None, f"Skill '{name}' not found"

        # Mark as active
        self.active_skills[name] = skill

        return skill, ""

    def get_active_skills(self) -> list[Skill]:
        """Get currently active skills."""
        return list(self.active_skills.values())

    def format_for_injection(self) -> str:
        """Format active skills for system prompt injection."""
        if not self.active_skills:
            return ""

        sections = []
        for name, skill in self.active_skills.items():
            sections.append(f"## {skill.metadata.title or name}\n\n{skill.content}")

        return "\n\n".join(sections)

    def deactivate(self, name: str) -> bool:
        """Deactivate a skill."""
        if name in self.active_skills:
            del self.active_skills[name]
            return True
        return False

    def deactivate_all(self) -> None:
        """Deactivate all skills."""
        self.active_skills.clear()

    def prepare_injection_messages(
        self,
        skill_names: list[str],
    ) -> list[dict[str, Any]]:
        """
        Prepare skill injection messages.

        Returns list of user messages ready for injection.
        """
        messages = []

        for name in skill_names:
            skill, _error = self.invoke(name)
            if skill:
                msg = self.loader.format_for_injection(skill)
                messages.append(msg)

        return messages

    def get_stats(self) -> dict[str, Any]:
        """Get skill system statistics."""
        total_skills = len(self.registry.skills)
        active_skills = len(self.active_skills)
        active_tokens = sum(s.estimated_tokens() for s in self.active_skills.values())

        return {
            "total_skills": total_skills,
            "active_skills": active_skills,
            "active_tokens": active_tokens,
            "skills_by_category": self._count_by_category(),
        }

    def _count_by_category(self) -> dict[str, int]:
        """Count skills by category."""
        counts: dict[str, int] = {}

        for skill in self.registry.skills.values():
            category = skill.category or "Uncategorized"
            counts[category] = counts.get(category, 0) + 1

        return counts


class SkillWriter:
    """Create and update skill files."""

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        title: str,
        description: str,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[bool, str]:
        """
        Create a new skill.

        Args:
            name: Skill identifier (kebab-case)
            title: Display name
            description: One-line summary (max 250 chars)
            content: Full skill instructions (markdown)
            category: Optional category
            tags: Optional tags

        Returns:
            (success, error_or_path)
        """
        # Validate name
        if not self._is_valid_name(name):
            return False, "Invalid name (use kebab-case)"

        # Check if exists
        file_path = self.skills_dir / f"{name}.md"
        if file_path.exists():
            return False, f"Skill '{name}' already exists"

        # Truncate description
        if len(description) > 250:
            description = description[:247] + "..."

        # Build frontmatter
        frontmatter = self._build_frontmatter(
            name=name,
            title=title,
            description=description,
            category=category,
            tags=tags,
        )

        # Combine
        full_content = f"{frontmatter}\n\n{content}"

        # Write
        try:
            file_path.write_text(full_content, encoding="utf-8")
            return True, str(file_path)
        except Exception as e:
            return False, f"Failed to write: {e}"

    def _build_frontmatter(
        self,
        name: str,
        title: str,
        description: str,
        category: str | None,
        tags: list[str] | None,
    ) -> str:
        """Build skill frontmatter."""
        tags_str = ", ".join(tags) if tags else ""
        category_str = category or ""

        frontmatter = f"""---
name: {name}
title: {title}
description: {description}
category: {category_str}
tags: {tags_str}
---"""
        return frontmatter

    def _is_valid_name(self, name: str) -> bool:
        """Check if name is valid kebab-case."""
        if not name:
            return False

        return all(char.isalnum() or char == "-" for char in name)


def create_skill_template(category: str = "general") -> str:
    """Get template for new skill."""
    return """# Skill Instructions

[Describe what this skill does and when to use it]

## Capabilities

- [Capability 1]
- [Capability 2]

## Usage

[Step-by-step instructions or examples]

## Notes

[Additional context, limitations, or considerations]
"""
