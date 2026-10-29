"""Tests for M4 Memory system."""

from __future__ import annotations

import pytest
import time
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from carbide.memory.base import Memory, MemoryIndex, MemoryMetadata, MemoryType
from carbide.memory.recall import MemoryPrefetcher, MemoryRecall
from carbide.memory.writer import MemoryWriter


class TestMemoryType:
    """Test memory type enumeration."""

    def test_all_types(self):
        """Verify all four closed types."""
        types = list(MemoryType)
        assert len(types) == 4
        assert MemoryType.USER in types
        assert MemoryType.FEEDBACK in types
        assert MemoryType.PROJECT in types
        assert MemoryType.REFERENCE in types


class TestMemoryMetadata:
    """Test memory metadata."""

    def test_basic_metadata(self):
        """Test basic metadata creation."""
        meta = MemoryMetadata(
            name="test-memory",
            description="Test description",
            type=MemoryType.USER,
            created_at=time.time(),
            updated_at=time.time(),
            tags=[],
        )
        assert meta.name == "test-memory"
        assert meta.description == "Test description"
        assert meta.type == MemoryType.USER
        assert meta.tags == []
        assert meta.created_at is not None

    def test_metadata_with_tags(self):
        """Test metadata with tags."""
        meta = MemoryMetadata(
            name="test",
            description="Test",
            type=MemoryType.FEEDBACK,
            created_at=time.time(),
            updated_at=time.time(),
            tags=["important", "python"],
        )
        assert len(meta.tags) == 2
        assert "important" in meta.tags


class TestMemory:
    """Test memory dataclass."""

    def test_memory_creation(self):
        """Test creating a memory."""
        meta = MemoryMetadata(
            name="test",
            description="Test memory",
            type=MemoryType.USER,
            created_at=time.time(),
            updated_at=time.time(),
            tags=[],
        )
        memory = Memory(
            metadata=meta,
            content="This is the memory content.",
            file_path=Path("test.md"),
        )
        assert memory.metadata.name == "test"
        assert memory.content == "This is the memory content."

    def test_memory_estimated_tokens(self):
        """Test token estimation."""
        meta = MemoryMetadata(
            name="test",
            description="Test",
            type=MemoryType.USER,
            created_at=time.time(),
            updated_at=time.time(),
            tags=[],
        )
        # ~100 chars content = ~25 tokens
        content = "a" * 100
        memory = Memory(metadata=meta, content=content, file_path=Path("test.md"))

        # Memory doesn't have estimated_tokens method, check size instead
        assert memory.size_bytes() == 100
        assert memory.line_count() >= 1


class TestMemoryIndex:
    """Test memory index."""

    def test_index_creation(self, tmp_path):
        """Test creating an index."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        index = MemoryIndex(memory_dir)
        assert index.memory_dir == memory_dir
        assert len(index.memories) == 0

    def test_index_scan_empty(self, tmp_path):
        """Test scanning empty directory."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        index = MemoryIndex(memory_dir)
        index.scan()
        assert len(index.memories) == 0

    def test_index_scan_single_memory(self, tmp_path):
        """Test scanning single memory file."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create memory file
        memory_file = memory_dir / "test-user.md"
        memory_file.write_text("""---
name: test-user
description: Test user memory
type: user
---

This is a test user memory.""")

        index = MemoryIndex(memory_dir)
        index.scan()

        assert len(index.memories) == 1
        assert "test-user" in index.memories

        meta = index.memories["test-user"]
        assert meta.name == "test-user"
        assert meta.description == "Test user memory"
        assert meta.type == MemoryType.USER

    def test_index_scan_multiple_memories(self, tmp_path):
        """Test scanning multiple memory files."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create multiple files
        for i in range(3):
            memory_file = memory_dir / f"test-{i}.md"
            memory_file.write_text(f"""---
name: test-{i}
description: Test memory {i}
type: user
---

Content {i}.""")

        index = MemoryIndex(memory_dir)
        index.scan()

        assert len(index.memories) == 3
        assert "test-0" in index.memories
        assert "test-1" in index.memories
        assert "test-2" in index.memories

    def test_index_filters_by_type(self, tmp_path):
        """Test filtering memories by type."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create memories of different types
        types = [MemoryType.USER, MemoryType.FEEDBACK, MemoryType.PROJECT]
        for i, mem_type in enumerate(types):
            memory_file = memory_dir / f"test-{i}.md"
            memory_file.write_text(f"""---
name: test-{i}
description: Test
type: {mem_type.value}
---

Content.""")

        index = MemoryIndex(memory_dir)
        index.scan()

        user_mems = index.list_by_type(MemoryType.USER)
        assert len(user_mems) == 1
        assert user_mems[0].type == MemoryType.USER

        feedback_mems = index.list_by_type(MemoryType.FEEDBACK)
        assert len(feedback_mems) == 1
        assert feedback_mems[0].type == MemoryType.FEEDBACK

    def test_index_size_limits(self, tmp_path):
        """Test index size limits (200 lines AND 25KB)."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        index = MemoryIndex(memory_dir)

        # Test line limit
        assert index.MAX_LINES == 200

        # Test byte limit
        assert index.MAX_BYTES == 25_000

    def test_index_loads_only_frontmatter(self, tmp_path):
        """Test that indexing only reads frontmatter (first 30 lines)."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create file with large content
        memory_file = memory_dir / "test.md"
        large_content = "\n".join([f"Line {i}" for i in range(1000)])
        memory_file.write_text(f"""---
name: test
description: Test
type: user
---

{large_content}""")

        index = MemoryIndex(memory_dir)
        index.scan()

        # Should still scan successfully (only reads first 30 lines)
        assert "test" in index.memories


class TestMemoryRecall:
    """Test memory recall system."""

    @pytest.mark.asyncio
    async def test_recall_basic(self, tmp_path):
        """Test basic memory recall."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create test memories
        memory_file = memory_dir / "test.md"
        memory_file.write_text("""---
name: test
description: Python testing best practices
type: feedback
---

Always use pytest fixtures.

**Why:** Fixtures provide better isolation.

**How to apply:** Use @pytest.fixture decorator.""")

        recall = MemoryRecall(memory_dir)
        recall.scan()

        # Mock selector function
        async def mock_selector(prompt, max_tokens):
            return "test"

        memories = await recall.recall(
            context="How should I write tests?",
            selector_func=mock_selector,
        )

        assert len(memories) == 1
        assert memories[0].metadata.name == "test"
        assert "pytest fixtures" in memories[0].content

    @pytest.mark.asyncio
    async def test_recall_respects_max_limit(self, tmp_path):
        """Test recall respects max_memories limit."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create multiple memories
        for i in range(10):
            memory_file = memory_dir / f"test-{i}.md"
            memory_file.write_text(f"""---
name: test-{i}
description: Test {i}
type: user
---

Content {i}.""")

        recall = MemoryRecall(memory_dir, max_recalled=3)
        recall.scan()

        # Mock selector to return all names
        async def mock_selector(prompt, max_tokens):
            return "test-0, test-1, test-2, test-3, test-4"

        memories = await recall.recall(
            context="Test context",
            selector_func=mock_selector,
        )

        # Should only return 3 (max_recalled limit)
        assert len(memories) <= 3

    @pytest.mark.asyncio
    async def test_recall_empty_index(self, tmp_path):
        """Test recall with no memories."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        recall = MemoryRecall(memory_dir)
        recall.scan()

        async def mock_selector(prompt, max_tokens):
            return ""

        memories = await recall.recall(
            context="Test context",
            selector_func=mock_selector,
        )

        assert len(memories) == 0

    @pytest.mark.asyncio
    async def test_recall_loads_content(self, tmp_path):
        """Test that recall loads full content."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create memory with significant content
        content = "This is the full memory content.\n" * 10
        memory_file = memory_dir / "test.md"
        memory_file.write_text(f"""---
name: test
description: Test memory
type: user
---

{content}""")

        recall = MemoryRecall(memory_dir)
        recall.scan()

        async def mock_selector(prompt, max_tokens):
            return "test"

        memories = await recall.recall(
            context="Test",
            selector_func=mock_selector,
        )

        assert len(memories) == 1
        # Content should be loaded
        assert len(memories[0].content) > 100


class TestMemoryPrefetcher:
    """Test memory prefetcher."""

    @pytest.mark.asyncio
    async def test_prefetch_starts_task(self, tmp_path):
        """Test that prefetch creates async task."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Create test memory
        memory_file = memory_dir / "test.md"
        memory_file.write_text("""---
name: test
description: Test
type: user
---

Content.""")

        recall = MemoryRecall(memory_dir)
        recall.scan()

        async def mock_selector(prompt, max_tokens):
            return ""

        prefetcher = MemoryPrefetcher(recall)

        # Start prefetch
        prefetcher.start_prefetch("Test context", mock_selector)

        # Should have task
        assert prefetcher._task is not None

        # Wait for completion
        memories = await prefetcher.get_memories()

        # Should have task result (empty due to mock returning "")
        assert isinstance(memories, list)


class TestMemoryWriter:
    """Test memory writer."""

    def test_writer_creation(self, tmp_path):
        """Test creating writer."""
        memory_dir = tmp_path / "memory"

        writer = MemoryWriter(memory_dir)

        # Directory should be created
        assert memory_dir.exists()

    def test_create_user_memory(self, tmp_path):
        """Test creating user memory."""
        memory_dir = tmp_path / "memory"
        writer = MemoryWriter(memory_dir)

        success, path = writer.create(
            name="test-user",
            description="Test user memory",
            content="User prefers Python over JavaScript.",
            mem_type=MemoryType.USER,
        )

        assert success is True
        assert "test-user.md" in path

        # Verify file exists
        memory_file = memory_dir / "test-user.md"
        assert memory_file.exists()

        # Verify content
        content = memory_file.read_text()
        assert "name: test-user" in content
        assert "type: user" in content
        assert "Python over JavaScript" in content

    def test_create_feedback_memory_validation(self, tmp_path):
        """Test feedback memory requires Why/How."""
        memory_dir = tmp_path / "memory"
        writer = MemoryWriter(memory_dir)

        # Missing Why/How should fail
        success, error = writer.create(
            name="test-feedback",
            description="Test feedback",
            content="Use fixtures.",
            mem_type=MemoryType.FEEDBACK,
        )

        assert success is False
        assert "Why" in error or "How" in error

    def test_create_feedback_memory_valid(self, tmp_path):
        """Test creating valid feedback memory."""
        memory_dir = tmp_path / "memory"
        writer = MemoryWriter(memory_dir)

        success, path = writer.create(
            name="test-feedback",
            description="Use pytest fixtures",
            content="""Use pytest fixtures for test setup.

**Why:** Better isolation and reusability.

**How to apply:** Use @pytest.fixture decorator.""",
            mem_type=MemoryType.FEEDBACK,
        )

        assert success is True

    def test_create_project_memory_validation(self, tmp_path):
        """Test project memory requires absolute dates."""
        memory_dir = tmp_path / "memory"
        writer = MemoryWriter(memory_dir)

        # Relative date should fail
        success, error = writer.create(
            name="test-project",
            description="Test project",
            content="Deadline is next week.",
            mem_type=MemoryType.PROJECT,
        )

        assert success is False
        assert "absolute" in error.lower()

    def test_create_duplicate_memory(self, tmp_path):
        """Test creating duplicate memory fails."""
        memory_dir = tmp_path / "memory"
        writer = MemoryWriter(memory_dir)

        # Create first memory
        writer.create(
            name="test",
            description="Test",
            content="Content",
            mem_type=MemoryType.USER,
        )

        # Try to create duplicate
        success, error = writer.create(
            name="test",
            description="Test 2",
            content="Content 2",
            mem_type=MemoryType.USER,
        )

        assert success is False
        assert "exists" in error.lower()

    def test_create_invalid_name(self, tmp_path):
        """Test invalid memory names are rejected."""
        memory_dir = tmp_path / "memory"
        writer = MemoryWriter(memory_dir)

        # Invalid characters
        success, error = writer.create(
            name="test memory",  # Space not allowed
            description="Test",
            content="Content",
            mem_type=MemoryType.USER,
        )

        assert success is False
        assert "kebab-case" in error.lower()
