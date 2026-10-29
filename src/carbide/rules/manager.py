"""Rule manager: handles rule activation and injection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from carbide.rules.base import Rule, RuleContext, RuleRegistry, RuleTrigger


class RuleManager:
    """
    Manages rule lifecycle: discovery, evaluation, injection.

    Key features:
    - Context-aware rule activation
    - Priority-based rule selection
    - Token budget tracking
    - Cache-friendly injection
    """

    def __init__(self, rules_dir: Path, max_rules: int = 10, max_tokens: int = 5000):
        self.rules_dir = rules_dir
        self.max_rules = max_rules
        self.max_tokens = max_tokens

        self.registry = RuleRegistry(rules_dir)

        # Track active rules for this session
        self.active_rules: list[Rule] = []

    def scan(self) -> None:
        """Scan and register all rules."""
        self.registry.scan()

    def evaluate(self, context: RuleContext) -> list[Rule]:
        """
        Evaluate rules against current context.

        Returns:
            List of matching rules, sorted by priority
        """
        # Get all matching rules
        matching = self.registry.get_active_rules(context)

        # Apply limits
        selected = self._apply_limits(matching)

        # Update active rules
        self.active_rules = selected

        return selected

    def _apply_limits(self, rules: list[Rule]) -> list[Rule]:
        """Apply max_rules and max_tokens limits."""
        selected: list[Rule] = []
        total_tokens = 0

        for rule in rules:
            # Check count limit
            if len(selected) >= self.max_rules:
                break

            # Check token budget
            rule_tokens = rule.estimated_tokens()
            if total_tokens + rule_tokens > self.max_tokens:
                # Skip this rule if it would exceed budget
                continue

            selected.append(rule)
            total_tokens += rule_tokens

        return selected

    def get_active_rules(self) -> list[Rule]:
        """Get currently active rules."""
        return self.active_rules

    def format_for_injection(self) -> str:
        """Format active rules for injection."""
        if not self.active_rules:
            return ""

        return self.registry.format_for_injection(self.active_rules)

    def get_stats(self) -> dict[str, Any]:
        """Get rule system statistics."""
        total_rules = len(self.registry.rules)
        active_rules = len(self.active_rules)
        active_tokens = sum(r.estimated_tokens() for r in self.active_rules)

        return {
            "total_rules": total_rules,
            "active_rules": active_rules,
            "active_tokens": active_tokens,
            "max_rules": self.max_rules,
            "max_tokens": self.max_tokens,
        }

    def build_context(
        self,
        active_files: list[str],
        working_directory: Path | None,
        task_description: str,
    ) -> RuleContext:
        """Build context for rule evaluation."""
        return RuleContext(
            active_files=active_files,
            working_directory=working_directory,
            task_description=task_description,
        )


def create_default_rules(rules_dir: Path) -> None:
    """Create default rules for common scenarios."""
    from carbide.rules.base import RuleWriter

    rules_dir.mkdir(parents=True, exist_ok=True)
    writer = RuleWriter(rules_dir)

    # Rule 1: Test file conventions
    writer.create(
        name="test-conventions",
        description="Testing best practices and conventions",
        content="""# Testing Conventions

When writing tests:
- Use descriptive test names that explain what is being tested
- Follow Arrange-Act-Assert pattern
- One assertion per test when possible
- Use fixtures for common setup
- Mock external dependencies

When fixing failing tests:
- Read the test name and assertions first
- Understand what behavior is expected
- Check if the test itself is correct before changing code
""",
        conditions=[
            (RuleTrigger.FILE_PATTERN, "tests/**/*.py"),
            (RuleTrigger.TASK_TYPE, ["test", "testing", "pytest"]),
        ],
        priority=10,
    )

    # Rule 2: Documentation updates
    writer.create(
        name="docs-accuracy",
        description="Keep documentation accurate and up-to-date",
        content="""# Documentation Standards

When updating code that has documentation:
- Update docstrings to reflect behavior changes
- Update README if public API changes
- Update inline comments if complex logic changes
- Remove outdated TODO comments

Documentation should be:
- Accurate (matches actual behavior)
- Concise (no redundant information)
- Helpful (explains why, not just what)
""",
        conditions=[
            (RuleTrigger.FILE_PATTERN, "*.py"),
        ],
        priority=5,
    )

    # Rule 3: Error handling
    writer.create(
        name="error-handling",
        description="Proper error handling and validation",
        content="""# Error Handling

When adding error handling:
- Catch specific exceptions, not bare `except:`
- Provide helpful error messages
- Log errors with context
- Clean up resources in finally blocks
- Consider returning Result types instead of raising

When validating inputs:
- Validate early (fail fast)
- Provide clear validation errors
- Document validation requirements
""",
        conditions=[
            (RuleTrigger.TASK_TYPE, ["error", "exception", "validation", "fix bug"]),
        ],
        priority=8,
    )

    # Rule 4: Performance considerations
    writer.create(
        name="performance",
        description="Performance best practices",
        content="""# Performance Considerations

When optimizing:
- Measure first (don't guess)
- Profile to find bottlenecks
- Consider algorithmic complexity
- Avoid premature optimization

Common optimizations:
- Use list comprehensions over loops where appropriate
- Avoid repeated lookups (cache results)
- Use generators for large sequences
- Batch database queries
- Consider async/await for I/O bound operations
""",
        conditions=[
            (RuleTrigger.TASK_TYPE, ["performance", "optimize", "slow", "speed up"]),
        ],
        priority=7,
    )

    # Rule 5: Security awareness
    writer.create(
        name="security",
        description="Security best practices",
        content="""# Security Considerations

When handling user input:
- Validate and sanitize all inputs
- Use parameterized queries (never string concatenation)
- Escape output appropriately
- Check file paths are within allowed boundaries

When handling sensitive data:
- Never log secrets or credentials
- Use environment variables for configuration
- Encrypt sensitive data at rest
- Use secure random for cryptographic purposes

When executing commands:
- Validate all inputs
- Use subprocess with list arguments (not shell=True)
- Set appropriate timeouts
- Limit resource usage
""",
        conditions=[
            (RuleTrigger.TASK_TYPE, ["security", "auth", "password", "credentials"]),
        ],
        priority=10,
    )
