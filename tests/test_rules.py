"""Tests for Rules system."""

from __future__ import annotations

import pytest
from pathlib import Path

from carbide.rules.base import (
    Rule,
    RuleCondition,
    RuleContext,
    RuleRegistry,
    RuleTrigger,
    RuleWriter,
)
from carbide.rules.manager import RuleManager


class TestRuleTrigger:
    """Test rule trigger enumeration."""

    def test_all_triggers(self):
        """Verify all trigger types."""
        triggers = list(RuleTrigger)
        assert len(triggers) == 5
        assert RuleTrigger.ALWAYS in triggers
        assert RuleTrigger.FILE_PATTERN in triggers
        assert RuleTrigger.DIRECTORY in triggers
        assert RuleTrigger.TASK_TYPE in triggers


class TestRuleCondition:
    """Test rule conditions."""

    def test_always_condition(self):
        """Test ALWAYS trigger."""
        cond = RuleCondition(trigger=RuleTrigger.ALWAYS)
        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )
        assert cond.matches(context) is True

    def test_file_pattern_match(self):
        """Test file pattern matching."""
        cond = RuleCondition(
            trigger=RuleTrigger.FILE_PATTERN,
            pattern="**/*.py",
        )
        context = RuleContext(
            active_files=["tests/test_foo.py", "src/main.py"],
            working_directory=None,
            task_description="",
        )
        assert cond.matches(context) is True

    def test_file_pattern_no_match(self):
        """Test file pattern not matching."""
        cond = RuleCondition(
            trigger=RuleTrigger.FILE_PATTERN,
            pattern="tests/**/*.py",
        )
        context = RuleContext(
            active_files=["src/main.py"],
            working_directory=None,
            task_description="",
        )
        assert cond.matches(context) is False

    def test_directory_match(self):
        """Test directory matching."""
        cond = RuleCondition(
            trigger=RuleTrigger.DIRECTORY,
            pattern="*/docs/*",
        )
        context = RuleContext(
            active_files=[],
            working_directory=Path("/project/docs/api"),
            task_description="",
        )
        assert cond.matches(context) is True

    def test_task_type_match(self):
        """Test task type keyword matching."""
        cond = RuleCondition(
            trigger=RuleTrigger.TASK_TYPE,
            keywords=["test", "testing"],
        )
        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="Fix the failing test",
        )
        assert cond.matches(context) is True

    def test_task_type_no_match(self):
        """Test task type not matching."""
        cond = RuleCondition(
            trigger=RuleTrigger.TASK_TYPE,
            keywords=["test", "testing"],
        )
        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="Add a new feature",
        )
        assert cond.matches(context) is False


class TestRule:
    """Test rule dataclass."""

    def test_rule_creation(self):
        """Test creating a rule."""
        rule = Rule(
            name="test-rule",
            description="A test rule",
            conditions=[],
            content="Rule content here",
            priority=5,
        )
        assert rule.name == "test-rule"
        assert rule.priority == 5
        assert rule.enabled is True

    def test_rule_matches_no_conditions(self):
        """Test rule with no conditions always matches."""
        rule = Rule(
            name="test",
            description="Test",
            conditions=[],
            content="Content",
        )
        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )
        assert rule.matches(context) is True

    def test_rule_matches_all_conditions(self):
        """Test rule matches when all conditions match."""
        rule = Rule(
            name="test",
            description="Test",
            conditions=[
                RuleCondition(trigger=RuleTrigger.FILE_PATTERN, pattern="*.py"),
                RuleCondition(trigger=RuleTrigger.TASK_TYPE, keywords=["test"]),
            ],
            content="Content",
        )
        context = RuleContext(
            active_files=["test.py"],
            working_directory=None,
            task_description="Run tests",
        )
        assert rule.matches(context) is True

    def test_rule_no_match_partial(self):
        """Test rule doesn't match if one condition fails."""
        rule = Rule(
            name="test",
            description="Test",
            conditions=[
                RuleCondition(trigger=RuleTrigger.FILE_PATTERN, pattern="*.py"),
                RuleCondition(trigger=RuleTrigger.TASK_TYPE, keywords=["test"]),
            ],
            content="Content",
        )
        context = RuleContext(
            active_files=["test.py"],
            working_directory=None,
            task_description="Add feature",  # Doesn't match
        )
        assert rule.matches(context) is False

    def test_rule_disabled(self):
        """Test disabled rule never matches."""
        rule = Rule(
            name="test",
            description="Test",
            conditions=[],
            content="Content",
            enabled=False,
        )
        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )
        assert rule.matches(context) is False

    def test_rule_estimated_tokens(self):
        """Test token estimation."""
        content = "a" * 400
        rule = Rule(
            name="test",
            description="Test",
            conditions=[],
            content=content,
        )
        tokens = rule.estimated_tokens()
        assert 80 <= tokens <= 120


class TestRuleRegistry:
    """Test rule registry."""

    def test_registry_creation(self, tmp_path):
        """Test creating a registry."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        registry = RuleRegistry(rules_dir)
        assert registry.rules_dir == rules_dir
        assert len(registry.rules) == 0

    def test_registry_scan_empty(self, tmp_path):
        """Test scanning empty directory."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        registry = RuleRegistry(rules_dir)
        registry.scan()
        assert len(registry.rules) == 0

    def test_registry_scan_single_rule(self, tmp_path):
        """Test scanning single rule."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create rule file
        rule_file = rules_dir / "test-rule.md"
        rule_file.write_text("""---
name: test-rule
description: A test rule
priority: 5
enabled: true
when: always
---

# Test Rule

This is the rule content.""")

        registry = RuleRegistry(rules_dir)
        registry.scan()

        assert len(registry.rules) == 1
        assert "test-rule" in registry.rules

        rule = registry.rules["test-rule"]
        assert rule.description == "A test rule"
        assert rule.priority == 5
        assert "rule content" in rule.content

    def test_registry_get_active_rules(self, tmp_path):
        """Test getting active rules based on context."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create multiple rules
        rule1_file = rules_dir / "always.md"
        rule1_file.write_text("""---
name: always
description: Always active
when: always
priority: 1
---

Content 1""")

        rule2_file = rules_dir / "tests.md"
        rule2_file.write_text("""---
name: tests
description: Test files
file_pattern: tests/*.py
priority: 10
---

Content 2""")

        registry = RuleRegistry(rules_dir)
        registry.scan()

        # Context matching both rules
        context = RuleContext(
            active_files=["tests/test_foo.py"],
            working_directory=None,
            task_description="",
        )

        active = registry.get_active_rules(context)
        assert len(active) == 2
        # Should be sorted by priority (highest first)
        assert active[0].name == "tests"
        assert active[1].name == "always"

    def test_registry_format_for_injection(self, tmp_path):
        """Test formatting rules for injection."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        rule_file = rules_dir / "test.md"
        rule_file.write_text("""---
name: test
description: Test
when: always
---

Rule content here.""")

        registry = RuleRegistry(rules_dir)
        registry.scan()

        rules = list(registry.rules.values())
        formatted = registry.format_for_injection(rules)

        assert "Rule: test" in formatted
        assert "Rule content here" in formatted


class TestRuleWriter:
    """Test rule writer."""

    def test_writer_creation(self, tmp_path):
        """Test creating writer."""
        rules_dir = tmp_path / "rules"

        writer = RuleWriter(rules_dir)

        # Directory should be created
        assert rules_dir.exists()

    def test_create_rule(self, tmp_path):
        """Test creating a rule."""
        rules_dir = tmp_path / "rules"
        writer = RuleWriter(rules_dir)

        success, path = writer.create(
            name="test-rule",
            description="A test rule",
            content="Rule content",
            priority=5,
        )

        assert success is True
        assert "test-rule.md" in path

        # Verify file exists
        rule_file = rules_dir / "test-rule.md"
        assert rule_file.exists()

        # Verify content
        content = rule_file.read_text()
        assert "name: test-rule" in content
        assert "priority: 5" in content
        assert "Rule content" in content

    def test_create_rule_with_conditions(self, tmp_path):
        """Test creating rule with conditions."""
        rules_dir = tmp_path / "rules"
        writer = RuleWriter(rules_dir)

        success, path = writer.create(
            name="test",
            description="Test",
            content="Content",
            conditions=[
                (RuleTrigger.FILE_PATTERN, "*.py"),
                (RuleTrigger.TASK_TYPE, ["test", "testing"]),
            ],
            priority=10,
        )

        assert success is True

        # Verify frontmatter
        rule_file = rules_dir / "test.md"
        content = rule_file.read_text()
        assert "file_pattern: *.py" in content
        assert "task_keywords: test, testing" in content

    def test_create_duplicate_rule(self, tmp_path):
        """Test creating duplicate rule fails."""
        rules_dir = tmp_path / "rules"
        writer = RuleWriter(rules_dir)

        # Create first rule
        writer.create(
            name="test",
            description="Test",
            content="Content",
        )

        # Try to create duplicate
        success, error = writer.create(
            name="test",
            description="Test 2",
            content="Content 2",
        )

        assert success is False
        assert "exists" in error.lower()

    def test_create_invalid_name(self, tmp_path):
        """Test invalid rule names are rejected."""
        rules_dir = tmp_path / "rules"
        writer = RuleWriter(rules_dir)

        # Invalid characters
        success, error = writer.create(
            name="test rule",  # Space not allowed
            description="Test",
            content="Content",
        )

        assert success is False
        assert "kebab-case" in error.lower()


class TestRuleManager:
    """Test rule manager."""

    def test_manager_creation(self, tmp_path):
        """Test creating manager."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        manager = RuleManager(rules_dir)
        assert manager.rules_dir == rules_dir
        assert len(manager.active_rules) == 0

    def test_manager_scan(self, tmp_path):
        """Test scanning for rules."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create rule
        rule_file = rules_dir / "test.md"
        rule_file.write_text("""---
name: test
description: Test rule
when: always
---

Content.""")

        manager = RuleManager(rules_dir)
        manager.scan()

        assert len(manager.registry.rules) == 1

    def test_manager_evaluate(self, tmp_path):
        """Test evaluating rules against context."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create rules
        rule1_file = rules_dir / "always.md"
        rule1_file.write_text("""---
name: always
description: Always
when: always
priority: 5
---

Content 1""")

        rule2_file = rules_dir / "tests.md"
        rule2_file.write_text("""---
name: tests
description: Tests
file_pattern: tests/*.py
priority: 10
---

Content 2""")

        manager = RuleManager(rules_dir)
        manager.scan()

        # Evaluate with context matching both
        context = RuleContext(
            active_files=["tests/test_foo.py"],
            working_directory=None,
            task_description="",
        )

        active = manager.evaluate(context)

        assert len(active) == 2
        assert active[0].name == "tests"  # Higher priority first

    def test_manager_apply_limits(self, tmp_path):
        """Test max_rules limit."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create multiple rules
        for i in range(5):
            rule_file = rules_dir / f"rule-{i}.md"
            rule_file.write_text(f"""---
name: rule-{i}
description: Rule {i}
when: always
priority: {i}
---

Content {i}""")

        manager = RuleManager(rules_dir, max_rules=3)
        manager.scan()

        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )

        active = manager.evaluate(context)

        # Should only return 3 rules (highest priority)
        assert len(active) == 3
        assert active[0].name == "rule-4"
        assert active[1].name == "rule-3"
        assert active[2].name == "rule-2"

    def test_manager_token_limit(self, tmp_path):
        """Test max_tokens limit."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create rules with different sizes
        rule1_file = rules_dir / "small.md"
        rule1_file.write_text("""---
name: small
description: Small
when: always
priority: 10
---

Small content.""")

        rule2_file = rules_dir / "large.md"
        large_content = "a" * 10000  # ~2500 tokens
        rule2_file.write_text(f"""---
name: large
description: Large
when: always
priority: 5
---

{large_content}""")

        manager = RuleManager(rules_dir, max_tokens=100)
        manager.scan()

        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )

        active = manager.evaluate(context)

        # Should only include small rule (large exceeds budget)
        assert len(active) == 1
        assert active[0].name == "small"

    def test_manager_format_for_injection(self, tmp_path):
        """Test formatting active rules."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        rule_file = rules_dir / "test.md"
        rule_file.write_text("""---
name: test
description: Test
when: always
---

Rule content.""")

        manager = RuleManager(rules_dir)
        manager.scan()

        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )

        manager.evaluate(context)
        formatted = manager.format_for_injection()

        assert "Rule: test" in formatted
        assert "Rule content" in formatted

    def test_manager_get_stats(self, tmp_path):
        """Test getting manager statistics."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create rules
        for i in range(3):
            rule_file = rules_dir / f"rule-{i}.md"
            rule_file.write_text(f"""---
name: rule-{i}
description: Rule {i}
when: always
---

Content {i}""")

        manager = RuleManager(rules_dir, max_rules=10, max_tokens=5000)
        manager.scan()

        context = RuleContext(
            active_files=[],
            working_directory=None,
            task_description="",
        )

        manager.evaluate(context)
        stats = manager.get_stats()

        assert stats["total_rules"] == 3
        assert stats["active_rules"] == 3
        assert stats["active_tokens"] > 0
        assert stats["max_rules"] == 10
        assert stats["max_tokens"] == 5000
