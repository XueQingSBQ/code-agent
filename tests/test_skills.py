"""Tests for M4 Skills system."""

from __future__ import annotations

from pathlib import Path

import pytest

from carbide.skills.base import Skill, SkillLoader, SkillMetadata, SkillRegistry
from carbide.skills.manager import SkillManager, SkillWriter, create_skill_template
from carbide.skills.paths import agent_skills_dir, carbide_home
from carbide.skills.tool import LoadSkillParams, LoadSkillTool
from carbide.tools.base import ToolContext


def test_agent_skill_paths_are_user_scoped(tmp_path, monkeypatch):
    """Agent skills resolve from Carbide's user home, not a project workspace."""
    monkeypatch.setenv("CARBIDE_HOME", str(tmp_path / "agent-home"))
    monkeypatch.delenv("CARBIDE_SKILLS_DIR", raising=False)

    assert carbide_home() == (tmp_path / "agent-home").resolve()
    assert agent_skills_dir() == (tmp_path / "agent-home" / "skills").resolve()

    explicit = tmp_path / "shared-skills"
    monkeypatch.setenv("CARBIDE_SKILLS_DIR", str(explicit))
    assert agent_skills_dir() == explicit.resolve()


class TestSkillMetadata:
    """Test skill metadata."""

    def test_basic_metadata(self):
        """Test basic metadata creation."""
        meta = SkillMetadata(
            name="test-skill",
            title="Test Skill",
            description="A test skill",
            category=None,
            tags=[],
        )
        assert meta.name == "test-skill"
        assert meta.title == "Test Skill"
        assert meta.description == "A test skill"
        assert meta.category is None
        assert meta.tags == []

    def test_metadata_with_category_and_tags(self):
        """Test metadata with category and tags."""
        meta = SkillMetadata(
            name="test",
            title="Test",
            description="Test skill",
            category="testing",
            tags=["python", "pytest"],
        )
        assert meta.category == "testing"
        assert len(meta.tags) == 2
        assert "python" in meta.tags


class TestSkill:
    """Test skill dataclass."""

    def test_skill_creation(self):
        """Test creating a skill."""
        meta = SkillMetadata(
            name="test",
            title="Test Skill",
            description="Test description",
            category=None,
            tags=[],
        )
        skill = Skill(
            metadata=meta,
            content="# Test Skill\n\nInstructions here.",
            file_path=Path("test.md"),
        )
        assert skill.metadata.name == "test"
        assert skill.content.startswith("# Test Skill")

    def test_skill_estimated_tokens(self):
        """Test token estimation."""
        meta = SkillMetadata(
            name="test",
            title="Test",
            description="Test",
            category=None,
            tags=[],
        )
        # ~400 chars content = ~100 tokens
        content = "a" * 400
        skill = Skill(metadata=meta, content=content, file_path=Path("test.md"))

        tokens = skill.estimated_tokens()
        # Should be ~100 (400/4)
        assert 80 <= tokens <= 120


class TestSkillRegistry:
    """Test skill registry."""

    def test_registry_creation(self, tmp_path):
        """Test creating a registry."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        registry = SkillRegistry(skills_dir)
        assert registry.skills_dir == skills_dir
        assert len(registry.skills) == 0

    def test_registry_scan_empty(self, tmp_path):
        """Test scanning empty directory."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        registry = SkillRegistry(skills_dir)
        registry.scan()
        assert len(registry.skills) == 0

    def test_registry_scan_single_skill(self, tmp_path):
        """Test scanning single skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill file
        skill_file = skills_dir / "test-skill.md"
        skill_file.write_text("""---
name: test-skill
title: Test Skill
description: A test skill for testing
---

# Test Skill

Instructions here.""")

        registry = SkillRegistry(skills_dir)
        registry.scan()

        assert len(registry.skills) == 1
        assert "test-skill" in registry.skills

        meta = registry.skills["test-skill"]
        assert meta.name == "test-skill"
        assert meta.title == "Test Skill"
        assert meta.description == "A test skill for testing"

    def test_registry_scan_multiple_skills(self, tmp_path):
        """Test scanning multiple skills."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create multiple skill files
        for i in range(3):
            skill_file = skills_dir / f"skill-{i}.md"
            skill_file.write_text(f"""---
name: skill-{i}
title: Skill {i}
description: Skill number {i}
---

Content.""")

        registry = SkillRegistry(skills_dir)
        registry.scan()

        assert len(registry.skills) == 3
        assert "skill-0" in registry.skills
        assert "skill-1" in registry.skills
        assert "skill-2" in registry.skills

    def test_registry_format_skill_list(self, tmp_path):
        """Test formatting skill list (Tier 1)."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skills
        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test Skill
description: A test skill
category: testing
---

Content.""")

        registry = SkillRegistry(skills_dir)
        registry.scan()

        skill_list = registry.format_skill_list()

        # Should contain skill name and description
        assert "test" in skill_list
        assert "A test skill" in skill_list
        assert "testing" in skill_list

    def test_registry_description_truncation(self, tmp_path):
        """Test description truncated to 250 chars."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill with long description
        long_desc = "a" * 300
        skill_file = skills_dir / "test.md"
        skill_file.write_text(f"""---
name: test
title: Test
description: {long_desc}
---

Content.""")

        registry = SkillRegistry(skills_dir)
        registry.scan()

        meta = registry.skills["test"]
        # Should be truncated to 250
        assert len(meta.description) == 250


class TestSkillLoader:
    """Test skill loader."""

    def test_loader_creation(self, tmp_path):
        """Test creating loader."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        loader = SkillLoader(skills_dir)
        assert loader.skills_dir == skills_dir

    def test_load_skill(self, tmp_path):
        """Test loading a skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill file
        skill_file = skills_dir / "test.md"
        skill_content = """---
name: test
title: Test Skill
description: Test description
---

# Test Skill

These are the instructions."""
        skill_file.write_text(skill_content)

        loader = SkillLoader(skills_dir)
        skill = loader.load("test")

        assert skill is not None
        assert skill.metadata.name == "test"
        assert "instructions" in skill.content

    def test_load_nonexistent_skill(self, tmp_path):
        """Test loading nonexistent skill returns None."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        loader = SkillLoader(skills_dir)
        skill = loader.load("nonexistent")

        assert skill is None

    def test_format_for_injection(self, tmp_path):
        """Test formatting skill for injection as user message."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill
        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test Skill
description: Test
---

# Instructions

Do this thing.""")

        loader = SkillLoader(skills_dir)
        skill = loader.load("test")

        message = loader.format_for_injection(skill)

        # Should be user message
        assert message["role"] == "user"
        # Check content structure (it's a list with text blocks)
        assert isinstance(message["content"], list)
        assert message["content"][0]["type"] == "text"
        assert "Test Skill" in message["content"][0]["text"]
        assert "Do this thing" in message["content"][0]["text"]

    def test_injection_preserves_cache(self, tmp_path):
        """Test that injection format preserves prompt cache."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test
description: Test
---

Content.""")

        loader = SkillLoader(skills_dir)
        skill = loader.load("test")

        message = loader.format_for_injection(skill)

        # Should be incremental user message (not system prompt modification)
        # This preserves prompt cache
        assert message["role"] == "user"
        # Content is a list with text blocks
        assert isinstance(message["content"], list)
        assert message["content"][0]["type"] == "text"


class TestSkillManager:
    """Test skill manager."""

    def test_manager_creation(self, tmp_path):
        """Test creating manager."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        manager = SkillManager(skills_dir)
        assert manager.skills_dir == skills_dir
        assert len(manager.active_skills) == 0

    def test_manager_scan(self, tmp_path):
        """Test scanning for skills."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill
        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test
description: Test skill
---

Content.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        skill_list = manager.get_skill_list()
        assert "test" in skill_list

    def test_manager_invoke_skill(self, tmp_path):
        """Test invoking a skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill
        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test
description: Test
---

Instructions.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        skill, error = manager.invoke("test")

        assert error == ""
        assert skill is not None
        assert skill.metadata.name == "test"
        # Should now be active
        assert "test" in manager.active_skills

    def test_manager_activates_slash_skill_only(self, tmp_path):
        """Only a registered Agent skill can be activated from a slash command."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "review.md").write_text(
            "---\nname: review\ntitle: Review\ndescription: Review code\n---\n\nInstructions."
        )

        manager = SkillManager(skills_dir)
        manager.scan()

        assert manager.activate_from_prompt("/review inspect this file") == "review"
        assert manager.activate_from_prompt("read /review in the docs") is None
        assert manager.activate_from_prompt("/missing do something") is None

    @pytest.mark.asyncio
    async def test_load_skill_tool_returns_full_skill(self, tmp_path):
        """The model-facing tool loads a registered skill through the tool contract."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "review.md").write_text(
            "---\nname: review\ntitle: Review\ndescription: Review code\n---\n\nFull instructions."
        )
        manager = SkillManager(skills_dir)
        manager.scan()

        result = await LoadSkillTool(manager).run(
            LoadSkillParams(name="review"), ToolContext(workspace=tmp_path)
        )

        assert not result.is_error
        assert "Full instructions." in result.content
        assert "review" in manager.active_skills

    def test_manager_invoke_caches_skill(self, tmp_path):
        """Test that invoking same skill uses cache."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill
        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test
description: Test
---

Content.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        # First invoke
        skill1, _ = manager.invoke("test")
        # Second invoke (should return cached)
        skill2, _ = manager.invoke("test")

        # Should be same object
        assert skill1 is skill2

    def test_manager_invoke_nonexistent(self, tmp_path):
        """Test invoking nonexistent skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        manager = SkillManager(skills_dir)

        skill, error = manager.invoke("nonexistent")

        assert skill is None
        assert "not found" in error

    def test_manager_deactivate_skill(self, tmp_path):
        """Test deactivating a skill."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skill
        skill_file = skills_dir / "test.md"
        skill_file.write_text("""---
name: test
title: Test
description: Test
---

Content.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        # Invoke then deactivate
        manager.invoke("test")
        assert "test" in manager.active_skills

        success = manager.deactivate("test")
        assert success is True
        assert "test" not in manager.active_skills

    def test_manager_deactivate_all(self, tmp_path):
        """Test deactivating all skills."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create multiple skills
        for i in range(3):
            skill_file = skills_dir / f"skill-{i}.md"
            skill_file.write_text(f"""---
name: skill-{i}
title: Skill {i}
description: Test
---

Content.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        # Invoke all
        for i in range(3):
            manager.invoke(f"skill-{i}")

        assert len(manager.active_skills) == 3

        # Deactivate all
        manager.deactivate_all()
        assert len(manager.active_skills) == 0

    def test_manager_prepare_injection_messages(self, tmp_path):
        """Test preparing injection messages."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skills
        for i in range(2):
            skill_file = skills_dir / f"skill-{i}.md"
            skill_file.write_text(f"""---
name: skill-{i}
title: Skill {i}
description: Test
---

Content {i}.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        messages = manager.prepare_injection_messages(["skill-0", "skill-1"])

        assert len(messages) == 2
        assert all(msg["role"] == "user" for msg in messages)
        # Check that skill names appear in the content
        all_text = "".join(
            block["text"] for msg in messages for block in msg["content"] if block["type"] == "text"
        )
        assert "skill-0" in all_text or "Skill 0" in all_text
        assert "skill-1" in all_text or "Skill 1" in all_text

    def test_manager_get_stats(self, tmp_path):
        """Test getting skill statistics."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create skills
        for i in range(3):
            skill_file = skills_dir / f"skill-{i}.md"
            skill_file.write_text(f"""---
name: skill-{i}
title: Skill {i}
description: Test
category: testing
---

Content.""")

        manager = SkillManager(skills_dir)
        manager.scan()

        # Activate one
        manager.invoke("skill-0")

        stats = manager.get_stats()

        assert stats["total_skills"] == 3
        assert stats["active_skills"] == 1
        assert stats["active_tokens"] > 0
        assert "testing" in stats["skills_by_category"]


class TestSkillWriter:
    """Test skill writer."""

    def test_writer_creation(self, tmp_path):
        """Test creating writer."""
        skills_dir = tmp_path / "skills"

        writer = SkillWriter(skills_dir)

        # Directory should be created
        assert writer.skills_dir == skills_dir
        assert skills_dir.exists()

    def test_create_skill(self, tmp_path):
        """Test creating a skill."""
        skills_dir = tmp_path / "skills"
        writer = SkillWriter(skills_dir)

        success, path = writer.create(
            name="test-skill",
            title="Test Skill",
            description="A test skill",
            content="# Test\n\nInstructions here.",
        )

        assert success is True
        assert "test-skill.md" in path

        # Verify file exists
        skill_file = skills_dir / "test-skill.md"
        assert skill_file.exists()

        # Verify content
        content = skill_file.read_text()
        assert "name: test-skill" in content
        assert "title: Test Skill" in content
        assert "Instructions here" in content

    def test_create_skill_with_category_and_tags(self, tmp_path):
        """Test creating skill with category and tags."""
        skills_dir = tmp_path / "skills"
        writer = SkillWriter(skills_dir)

        success, _path = writer.create(
            name="test",
            title="Test",
            description="Test skill",
            content="Content.",
            category="testing",
            tags=["python", "pytest"],
        )

        assert success is True

        # Verify frontmatter
        skill_file = skills_dir / "test.md"
        content = skill_file.read_text()
        assert "category: testing" in content
        assert "python, pytest" in content

    def test_create_duplicate_skill(self, tmp_path):
        """Test creating duplicate skill fails."""
        skills_dir = tmp_path / "skills"
        writer = SkillWriter(skills_dir)

        # Create first skill
        writer.create(
            name="test",
            title="Test",
            description="Test",
            content="Content",
        )

        # Try to create duplicate
        success, error = writer.create(
            name="test",
            title="Test 2",
            description="Test 2",
            content="Content 2",
        )

        assert success is False
        assert "exists" in error.lower()

    def test_create_invalid_name(self, tmp_path):
        """Test invalid skill names are rejected."""
        skills_dir = tmp_path / "skills"
        writer = SkillWriter(skills_dir)

        # Invalid characters
        success, error = writer.create(
            name="test skill",  # Space not allowed
            title="Test",
            description="Test",
            content="Content",
        )

        assert success is False
        assert "kebab-case" in error.lower()

    def test_description_truncation(self, tmp_path):
        """Test long descriptions are truncated."""
        skills_dir = tmp_path / "skills"
        writer = SkillWriter(skills_dir)

        long_desc = "a" * 300

        success, _path = writer.create(
            name="test",
            title="Test",
            description=long_desc,
            content="Content",
        )

        assert success is True

        # Verify description truncated
        skill_file = skills_dir / "test.md"
        content = skill_file.read_text()
        # Should have ellipsis
        assert "..." in content


class TestSkillTemplate:
    """Test skill template generation."""

    def test_create_skill_template(self):
        """Test creating skill template."""
        template = create_skill_template()

        assert "# Skill Instructions" in template
        assert "Capabilities" in template
        assert "Usage" in template
        assert "Notes" in template

    def test_create_skill_template_with_category(self):
        """Test creating template with category."""
        template = create_skill_template(category="testing")

        # Template doesn't use category directly, just verify it accepts it
        assert isinstance(template, str)
        assert len(template) > 0
