"""Rules system: conditional behavior based on file patterns and context.

Rules allow project-specific customizations that activate conditionally:
- File pattern matching (glob-based)
- Context triggers (e.g., "when editing tests", "when in docs/")
- Priority-based rule selection
- Rules injected as system prompt additions
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class RuleTrigger(StrEnum):
    """When a rule should activate."""

    ALWAYS = "always"  # Always active
    FILE_PATTERN = "file_pattern"  # When working with matching files
    DIRECTORY = "directory"  # When in specific directory
    TASK_TYPE = "task_type"  # Based on task keywords
    COMBINED = "combined"  # Multiple conditions (AND)


@dataclass
class RuleCondition:
    """A single condition for rule activation."""

    trigger: RuleTrigger
    pattern: str | None = None  # For file_pattern/directory
    keywords: list[str] | None = None  # For task_type

    def matches(
        self,
        context: RuleContext,
    ) -> bool:
        """Check if condition matches current context."""
        if self.trigger == RuleTrigger.ALWAYS:
            return True

        if self.trigger == RuleTrigger.FILE_PATTERN:
            if not self.pattern:
                return False
            return any(self._matches_pattern(f, self.pattern) for f in context.active_files)

        if self.trigger == RuleTrigger.DIRECTORY:
            if not self.pattern:
                return False
            cwd = context.working_directory
            if not cwd:
                return False
            return self._matches_pattern(str(cwd), self.pattern)

        if self.trigger == RuleTrigger.TASK_TYPE:
            if not self.keywords:
                return False
            task_lower = context.task_description.lower()
            return any(kw.lower() in task_lower for kw in self.keywords)

        return False

    def _matches_pattern(self, path: str, pattern: str) -> bool:
        """Match path against glob pattern."""
        from pathlib import PurePath

        # Normalize path separators
        normalized_path = path.replace("\\", "/")
        normalized_pattern = pattern.replace("\\", "/")

        # Use pathlib for glob matching (supports **)
        try:
            return PurePath(normalized_path).match(normalized_pattern)
        except Exception:
            return False


@dataclass
class Rule:
    """A conditional rule with content and metadata."""

    name: str  # Unique identifier
    description: str  # What this rule does
    conditions: list[RuleCondition]  # When to activate
    content: str  # Rule text (markdown)
    priority: int = 0  # Higher priority rules win on conflict
    enabled: bool = True

    def matches(self, context: RuleContext) -> bool:
        """Check if all conditions match (AND logic)."""
        if not self.enabled:
            return False

        if not self.conditions:
            return True  # No conditions = always match

        return all(cond.matches(context) for cond in self.conditions)

    def estimated_tokens(self) -> int:
        """Estimate token count."""
        return len(self.content) // 4


@dataclass
class RuleContext:
    """Context for rule evaluation."""

    active_files: list[str]  # Currently open/edited files
    working_directory: Path | None  # Current working directory
    task_description: str  # User's current task/prompt
    session_tags: list[str] | None = None  # Optional session tags


class RuleRegistry:
    """
    Registry of all available rules.

    Rules are loaded from .claude/rules/ directory.
    """

    def __init__(self, rules_dir: Path):
        self.rules_dir = rules_dir
        self.rules: dict[str, Rule] = {}

    def scan(self) -> None:
        """Scan rules directory and load all rules."""
        if not self.rules_dir.exists():
            return

        for file_path in self.rules_dir.glob("*.md"):
            try:
                rule = self._load_rule(file_path)
                if rule:
                    self.rules[rule.name] = rule
            except Exception:
                # Skip files that can't be parsed
                continue

    def _load_rule(self, file_path: Path) -> Rule | None:
        """Load a rule from markdown file."""
        try:
            content = file_path.read_text(encoding="utf-8")

            # Parse frontmatter
            if not content.startswith("---"):
                return None

            end_idx = content.find("---", 3)
            if end_idx == -1:
                return None

            frontmatter = content[3:end_idx].strip()
            body = content[end_idx + 3 :].strip()

            # Parse fields
            fields = {}
            for line in frontmatter.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip()

            # Required fields
            name = fields.get("name", file_path.stem)
            description = fields.get("description", "")
            priority = int(fields.get("priority", "0"))
            enabled = fields.get("enabled", "true").lower() == "true"

            # Parse conditions
            conditions = self._parse_conditions(fields)

            return Rule(
                name=name,
                description=description,
                conditions=conditions,
                content=body,
                priority=priority,
                enabled=enabled,
            )

        except Exception:
            return None

    def _parse_conditions(self, fields: dict[str, str]) -> list[RuleCondition]:
        """Parse conditions from frontmatter."""
        conditions = []

        # Check for 'when' field
        when = fields.get("when", "")
        if when == "always":
            conditions.append(RuleCondition(trigger=RuleTrigger.ALWAYS))
            return conditions

        # File pattern
        file_pattern = fields.get("file_pattern")
        if file_pattern:
            conditions.append(
                RuleCondition(
                    trigger=RuleTrigger.FILE_PATTERN,
                    pattern=file_pattern,
                )
            )

        # Directory
        directory = fields.get("directory")
        if directory:
            conditions.append(
                RuleCondition(
                    trigger=RuleTrigger.DIRECTORY,
                    pattern=directory,
                )
            )

        # Task type
        task_keywords = fields.get("task_keywords")
        if task_keywords:
            keywords = [k.strip() for k in task_keywords.split(",")]
            conditions.append(
                RuleCondition(
                    trigger=RuleTrigger.TASK_TYPE,
                    keywords=keywords,
                )
            )

        return conditions

    def get_active_rules(self, context: RuleContext) -> list[Rule]:
        """Get all rules that match the current context."""
        active = [rule for rule in self.rules.values() if rule.matches(context)]

        # Sort by priority (highest first)
        active.sort(key=lambda r: r.priority, reverse=True)

        return active

    def format_for_injection(self, rules: list[Rule]) -> str:
        """Format active rules for system prompt injection."""
        if not rules:
            return ""

        sections = []

        for rule in rules:
            section = f"""## Rule: {rule.name}

{rule.content}
"""
            sections.append(section)

        return "\n".join(sections)


class RuleWriter:
    """Create and update rule files."""

    def __init__(self, rules_dir: Path):
        self.rules_dir = rules_dir
        self.rules_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        description: str,
        content: str,
        conditions: list[tuple[RuleTrigger, str | list[str]]] | None = None,
        priority: int = 0,
    ) -> tuple[bool, str]:
        """
        Create a new rule.

        Args:
            name: Rule identifier (kebab-case)
            description: One-line summary
            content: Rule text (markdown)
            conditions: List of (trigger, pattern/keywords) tuples
            priority: Priority (higher wins)

        Returns:
            (success, error_or_path)
        """
        # Validate name
        if not self._is_valid_name(name):
            return False, "Invalid name (use kebab-case)"

        # Check if exists
        file_path = self.rules_dir / f"{name}.md"
        if file_path.exists():
            return False, f"Rule '{name}' already exists"

        # Build frontmatter
        frontmatter = self._build_frontmatter(
            name=name,
            description=description,
            conditions=conditions or [],
            priority=priority,
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
        description: str,
        conditions: list[tuple[RuleTrigger, str | list[str]]],
        priority: int,
    ) -> str:
        """Build rule frontmatter."""
        lines = [
            "---",
            f"name: {name}",
            f"description: {description}",
            f"priority: {priority}",
            "enabled: true",
        ]

        # Add conditions
        for trigger, value in conditions:
            if trigger == RuleTrigger.FILE_PATTERN:
                lines.append(f"file_pattern: {value}")
            elif trigger == RuleTrigger.DIRECTORY:
                lines.append(f"directory: {value}")
            elif trigger == RuleTrigger.TASK_TYPE:
                if isinstance(value, list):
                    lines.append(f"task_keywords: {', '.join(value)}")
            elif trigger == RuleTrigger.ALWAYS:
                lines.append("when: always")

        lines.append("---")
        return "\n".join(lines)

    def _is_valid_name(self, name: str) -> bool:
        """Check if name is valid kebab-case."""
        if not name:
            return False

        return all(char.isalnum() or char == "-" for char in name)


def create_rule_template() -> str:
    """Get template for new rule."""
    return """# Rule Content

[Describe the rule and what it enforces]

## Examples

[Optional: provide examples of what to do or avoid]

## Rationale

[Optional: explain why this rule exists]
"""
