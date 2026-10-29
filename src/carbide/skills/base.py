"""Skills system: three-tier progressive disclosure.

Tier 1: Skill list (names + descriptions, ~1% context)
Tier 2: Selected skill details (when invoked)
Tier 3: Full skill content (injected as user message, not system prompt)

Key optimization: Inject as incremental user messages to preserve prompt cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SkillMetadata:
    """Skill frontmatter metadata."""

    name: str  # Skill identifier (e.g., "code-review")
    title: str  # Display name (e.g., "Code Review")
    description: str  # One-line summary (max 250 chars)
    category: str | None = None  # Optional grouping
    tags: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "tags": self.tags or [],
        }


@dataclass
class Skill:
    """A skill with full content."""

    metadata: SkillMetadata
    content: str  # Full markdown instructions
    file_path: Path

    def size_bytes(self) -> int:
        """Get skill size in bytes."""
        return len(self.content.encode("utf-8"))

    def estimated_tokens(self) -> int:
        """Rough token estimate."""
        return self.size_bytes() // 4


class SkillRegistry:
    """
    Registry of available skills.

    Tier 1: Maintains lightweight list of all skills (name + description only).
    """

    MAX_DESCRIPTION_LENGTH = 250

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills: dict[str, SkillMetadata] = {}

    def scan(self) -> None:
        """Scan skills directory and build registry."""
        if not self.skills_dir.exists():
            return

        for file_path in self.skills_dir.glob("*.md"):
            try:
                metadata = self._parse_skill_metadata(file_path)
                if metadata:
                    self.skills[metadata.name] = metadata
            except Exception:
                continue

    def _parse_skill_metadata(self, file_path: Path) -> SkillMetadata | None:
        """Parse skill frontmatter (first 30 lines)."""
        try:
            with file_path.open(encoding="utf-8") as f:
                lines = [f.readline() for _ in range(30)]

            content = "".join(lines)

            if not content.startswith("---"):
                return None

            end_idx = content.find("---", 3)
            if end_idx == -1:
                return None

            frontmatter = content[3:end_idx].strip()

            # Parse fields
            fields = {}
            for line in frontmatter.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip()

            # Required fields
            name = fields.get("name") or file_path.stem
            title = fields.get("title", name)
            description = fields.get("description", "")

            # Truncate description
            if len(description) > self.MAX_DESCRIPTION_LENGTH:
                description = description[: self.MAX_DESCRIPTION_LENGTH - 3] + "..."

            # Optional fields
            category = fields.get("category")
            tags_str = fields.get("tags", "")
            tags = [t.strip() for t in tags_str.split(",") if t.strip()]

            return SkillMetadata(
                name=name,
                title=title,
                description=description,
                category=category,
                tags=tags,
            )

        except Exception:
            return None

    def get(self, name: str) -> SkillMetadata | None:
        """Get skill metadata by name."""
        return self.skills.get(name)

    def list_all(self) -> list[SkillMetadata]:
        """List all registered skills."""
        return list(self.skills.values())

    def search(self, query: str) -> list[SkillMetadata]:
        """Search skills by name/description."""
        query_lower = query.lower()
        return [
            s
            for s in self.skills.values()
            if query_lower in s.name.lower() or query_lower in s.description.lower()
        ]

    def format_skill_list(self) -> str:
        """
        Format skill list for Tier 1 disclosure.

        Optimized for minimal context usage (~1% of window).
        """
        if not self.skills:
            return ""

        # Group by category
        by_category: dict[str, list[SkillMetadata]] = {}
        uncategorized = []

        for skill in self.skills.values():
            if skill.category:
                if skill.category not in by_category:
                    by_category[skill.category] = []
                by_category[skill.category].append(skill)
            else:
                uncategorized.append(skill)

        lines = ["Available skills:"]

        # Categorized skills
        for category, skills in sorted(by_category.items()):
            lines.append(f"\n{category}:")
            for skill in sorted(skills, key=lambda s: s.name):
                lines.append(f"  /{skill.name} - {skill.description}")

        # Uncategorized skills
        if uncategorized:
            lines.append("\nOther:")
            for skill in sorted(uncategorized, key=lambda s: s.name):
                lines.append(f"  /{skill.name} - {skill.description}")

        return "\n".join(lines)


class SkillLoader:
    """
    Loads full skill content (Tier 2/3).

    Tier 2: Load when skill is invoked
    Tier 3: Full content injected as incremental user message
    """

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir

    def load(self, name: str) -> Skill | None:
        """Load full skill content."""
        file_path = self.skills_dir / f"{name}.md"
        if not file_path.exists():
            return None

        try:
            content = file_path.read_text(encoding="utf-8")

            # Parse frontmatter to get metadata
            metadata = self._parse_metadata(content, name)

            return Skill(
                metadata=metadata,
                content=content,
                file_path=file_path,
            )

        except Exception:
            return None

    def _parse_metadata(self, content: str, name: str) -> SkillMetadata:
        """Parse metadata from content."""
        if content.startswith("---"):
            end_idx = content.find("---", 3)
            if end_idx != -1:
                frontmatter = content[3:end_idx].strip()

                fields = {}
                for line in frontmatter.splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        fields[key.strip()] = value.strip()

                return SkillMetadata(
                    name=fields.get("name", name),
                    title=fields.get("title", name),
                    description=fields.get("description", ""),
                    category=fields.get("category"),
                    tags=[t.strip() for t in fields.get("tags", "").split(",") if t.strip()],
                )

        # Fallback
        return SkillMetadata(
            name=name,
            title=name,
            description="",
        )

    def format_for_injection(self, skill: Skill) -> dict[str, Any]:
        """
        Format skill for injection as user message (not system prompt).

        This preserves prompt cache by keeping system prompt static.
        """
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"[Skill: {skill.metadata.title}]\n\n{skill.content}",
                }
            ],
            "_skill_injection": True,
            "_skill_name": skill.metadata.name,
        }
