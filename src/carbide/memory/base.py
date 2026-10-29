"""Memory system: persistent knowledge with typed recall.

Four types: user (who), feedback (how), project (what), reference (where).
Three-step recall: scan frontmatter → LLM selector → inject as system-reminder.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class MemoryType(StrEnum):
    """Four closed memory types."""

    USER = "user"  # Who the user is (role, expertise, preferences)
    FEEDBACK = "feedback"  # How to work (corrections, confirmed approaches)
    PROJECT = "project"  # What we're building (goals, constraints, decisions)
    REFERENCE = "reference"  # Where to find things (URLs, dashboards, tickets)


_MEMORY_NAME_RE = re.compile(r"^[\w]+(?:-[\w]+)*$", re.UNICODE)


def is_valid_memory_name(name: object) -> bool:
    """Return whether *name* is safe to use as a memory filename.

    Memory names are deliberately stricter than arbitrary filesystem paths.
    This check is shared by the index and writer so a hand-edited file cannot
    make recall or update escape the memory directory.
    """
    return (
        isinstance(name, str)
        and 1 <= len(name) <= 80
        and _MEMORY_NAME_RE.fullmatch(name) is not None
    )


@dataclass
class MemoryMetadata:
    """Parsed frontmatter from a memory file."""

    name: str  # kebab-case slug
    description: str  # One-line summary for recall
    type: MemoryType
    created_at: float
    updated_at: float
    tags: list[str] | None = None
    confidence: float = 1.0
    source_session: str | None = None
    source_turn: int | None = None
    status: str = "active"
    supersedes: str | None = None
    last_confirmed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.type.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags or [],
            "confidence": self.confidence,
            "source_session": self.source_session,
            "source_turn": self.source_turn,
            "status": self.status,
            "supersedes": self.supersedes,
            "last_confirmed_at": self.last_confirmed_at,
        }


@dataclass
class Memory:
    """A single memory with content."""

    metadata: MemoryMetadata
    content: str  # Full markdown content
    file_path: Path

    @property
    def age_days(self) -> float:
        """Get age in days since last update."""
        return (time.time() - self.metadata.updated_at) / 86400

    def is_stale(self, threshold_days: int = 30) -> bool:
        """Check if memory is stale (>30 days old)."""
        return self.age_days > threshold_days

    def size_bytes(self) -> int:
        """Get memory size in bytes."""
        return len(self.content.encode("utf-8"))

    def line_count(self) -> int:
        """Get number of lines."""
        return len(self.content.splitlines())


class MemoryIndex:
    """
    Fast index of memory frontmatter.

    Only reads first 30 lines of each file for metadata.
    Enforces dual limits: 200 lines AND 25KB per memory.
    """

    MAX_LINES = 200
    MAX_BYTES = 25_000

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memories: dict[str, MemoryMetadata] = {}

    def scan(self) -> None:
        """Scan memory directory and build index."""
        # Rebuild the index so deletes and updates are visible immediately.
        self.memories.clear()
        if not self.memory_dir.exists():
            return

        for file_path in self.memory_dir.glob("*.md"):
            try:
                if file_path.is_symlink():
                    continue
                metadata = self._parse_frontmatter(file_path)
                if metadata:
                    self.memories[metadata.name] = metadata
            except Exception:
                # Skip files that can't be parsed
                continue

    def _parse_frontmatter(self, file_path: Path) -> MemoryMetadata | None:
        """Parse frontmatter from first 30 lines."""
        try:
            with file_path.open(encoding="utf-8") as f:
                lines = [f.readline() for _ in range(30)]

            content = "".join(lines)

            # Simple frontmatter parsing (---\nkey: value\n---)
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
            name = fields.get("name")
            description = fields.get("description")
            mem_type = fields.get("type")

            if not (name and description and mem_type):
                return None
            if not is_valid_memory_name(name) or file_path.stem != name:
                return None

            # Timestamps and optional provenance fields.  Invalid optional
            # values do not make an otherwise valid memory disappear.
            stats = file_path.stat()
            created_at = _parse_float(fields.get("created_at"), stats.st_ctime)
            updated_at = stats.st_mtime

            # Parse tags, including the simple ``[a, b]`` form.
            tags = _parse_tags(fields.get("tags", ""))
            confidence = _parse_float(fields.get("confidence"), 1.0)
            source_turn = _parse_int(fields.get("source_turn"))
            last_confirmed_at = _parse_float(fields.get("last_confirmed_at"))

            return MemoryMetadata(
                name=name,
                description=description,
                type=MemoryType(mem_type),
                created_at=created_at or 0.0,
                updated_at=updated_at,
                tags=tags,
                confidence=max(0.0, min(1.0, confidence or 0.0)),
                source_session=fields.get("source_session") or None,
                source_turn=source_turn,
                status=fields.get("status", "active") or "active",
                supersedes=fields.get("supersedes") or None,
                last_confirmed_at=last_confirmed_at,
            )

        except Exception:
            return None

    def get(self, name: str) -> MemoryMetadata | None:
        """Get metadata by name."""
        return self.memories.get(name)

    def list_by_type(self, mem_type: MemoryType) -> list[MemoryMetadata]:
        """List all memories of a specific type."""
        return [m for m in self.memories.values() if m.type == mem_type]

    def search(self, query: str) -> list[MemoryMetadata]:
        """Simple text search in descriptions."""
        query_lower = query.lower()
        return [
            m
            for m in self.memories.values()
            if query_lower in m.description.lower() or query_lower in m.name.lower()
        ]


def _parse_float(value: str | None, default: float | None = None) -> float | None:
    """Parse optional numeric frontmatter without rejecting the whole file."""
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _parse_int(value: str | None) -> int | None:
    """Parse an optional integer frontmatter value."""
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _parse_tags(value: str) -> list[str]:
    """Parse comma-separated tags and the simple ``[a, b]`` form."""
    normalized = value.strip().strip("[]")
    return [tag.strip().strip("'\"") for tag in normalized.split(",") if tag.strip()]


def validate_memory_file(file_path: Path) -> tuple[bool, str]:
    """
    Validate memory file meets constraints.

    Returns:
        (is_valid, error_message)
    """
    if not file_path.exists():
        return False, "File does not exist"

    # Check size
    size = file_path.stat().st_size
    if size > MemoryIndex.MAX_BYTES:
        return False, f"File too large: {size} bytes (max {MemoryIndex.MAX_BYTES})"

    # Check line count
    try:
        with file_path.open(encoding="utf-8") as f:
            text = f.read()

        line_count = len(text.splitlines())

        if line_count > MemoryIndex.MAX_LINES:
            return False, f"Too many lines: {line_count} (max {MemoryIndex.MAX_LINES})"

        # Validate the minimal on-disk contract before a file is made visible.
        # This intentionally remains a small parser; Markdown body content is
        # opaque and does not need to be interpreted here.
        if not text.startswith("---"):
            return False, "Missing frontmatter"
        end_idx = text.find("---", 3)
        if end_idx == -1:
            return False, "Unterminated frontmatter"

        fields: dict[str, str] = {}
        for line in text[3:end_idx].splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()

        if not fields.get("name") or not is_valid_memory_name(fields["name"]):
            return False, "Invalid or missing frontmatter name"
        if not fields.get("description"):
            return False, "Missing frontmatter description"
        try:
            MemoryType(fields.get("type", ""))
        except ValueError:
            return False, "Invalid or missing frontmatter type"

    except Exception as e:
        return False, f"Could not read file: {e}"

    return True, ""
