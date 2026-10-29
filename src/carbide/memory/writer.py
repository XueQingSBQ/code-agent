"""Memory writer: create and update memory files with validation.

Enforces memory type constraints and frontmatter requirements.
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from carbide.memory.base import MemoryType, is_valid_memory_name, validate_memory_file


class MemoryWriter:
    """
    Create and update memory files.

    Enforces:
    - Proper frontmatter format
    - Type-specific requirements (feedback needs Why/How)
    - Size limits (200 lines, 25KB)
    """

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        description: str,
        content: str,
        mem_type: MemoryType,
        tags: list[str] | None = None,
        confidence: float = 1.0,
        source_session: str | None = None,
        source_turn: int | None = None,
        status: str = "active",
        supersedes: str | None = None,
        last_confirmed_at: float | None = None,
    ) -> tuple[bool, str]:
        """
        Create a new memory file.

        Args:
            name: Kebab-case slug
            description: One-line summary
            content: Memory content (markdown)
            mem_type: Memory type
            tags: Optional tags

        Returns:
            (success, error_message_or_path)
        """
        if not self._is_valid_name(name):
            return False, "Invalid name (use kebab-case)"
        if not isinstance(description, str) or not description.strip():
            return False, "Description must not be empty"
        if not isinstance(content, str) or not content.strip():
            return False, "Content must not be empty"
        try:
            mem_type = MemoryType(mem_type)
        except (TypeError, ValueError):
            return False, "Invalid memory type"
        metadata_error = self._validate_metadata(
            description=description,
            tags=tags,
            confidence=confidence,
            source_session=source_session,
            source_turn=source_turn,
            status=status,
            supersedes=supersedes,
            last_confirmed_at=last_confirmed_at,
        )
        if metadata_error:
            return False, metadata_error

        # Check if exists
        file_path = self.memory_dir / f"{name}.md"
        if file_path.exists():
            return False, f"Memory '{name}' already exists"

        # Validate type-specific requirements
        if mem_type == MemoryType.FEEDBACK and not self._has_feedback_structure(content):
            return False, "Feedback memory must include '**Why:**' and '**How to apply:**'"

        # Check for relative dates that should be absolute.
        if mem_type == MemoryType.PROJECT and self._has_relative_dates(content):
            return (
                False,
                "Project memory should use absolute dates, not relative (e.g., '2024-03-15' not 'last week')",
            )

        # Build frontmatter
        frontmatter = self._build_frontmatter(
            name=name,
            description=description,
            mem_type=mem_type,
            tags=tags,
            confidence=confidence,
            source_session=source_session,
            source_turn=source_turn,
            status=status,
            supersedes=supersedes,
            last_confirmed_at=last_confirmed_at,
        )

        # Combine
        full_content = f"{frontmatter}\n\n{content}"

        # Write
        try:
            is_valid, error = self._atomic_write_validated(
                file_path, full_content, replace_existing=False
            )
            if not is_valid:
                return False, error

            return True, str(file_path)

        except Exception as e:
            return False, f"Failed to write: {e}"

    def update(
        self,
        name: str,
        content: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        source_session: str | None = None,
        source_turn: int | None = None,
        status: str | None = None,
        supersedes: str | None = None,
        last_confirmed_at: float | None = None,
    ) -> tuple[bool, str]:
        """
        Update existing memory.

        Args:
            name: Memory name
            content: New content (or None to keep)
            description: New description (or None to keep)
            tags: New tags (or None to keep)

        Returns:
            (success, error_message)
        """
        if not self._is_valid_name(name):
            return False, "Invalid name (use kebab-case)"
        file_path = self.memory_dir / f"{name}.md"
        if not file_path.exists():
            return False, f"Memory '{name}' not found"
        if file_path.is_symlink():
            return False, "Refusing to update a symbolic link"

        # Read existing
        try:
            existing = file_path.read_text(encoding="utf-8")

            # Parse existing frontmatter
            frontmatter_end = existing.find("---", 3)
            if frontmatter_end == -1:
                return False, "Invalid frontmatter"

            existing_content = existing[frontmatter_end + 3 :].strip()

            # Parse existing metadata
            frontmatter_text = existing[3:frontmatter_end]
            metadata = self._parse_frontmatter_fields(frontmatter_text)

            # Update fields
            if description is not None:
                if not isinstance(description, str) or not description.strip():
                    return False, "Description must not be empty"
                metadata["description"] = description
            if tags is not None:
                metadata["tags"] = tags
            if confidence is not None:
                metadata["confidence"] = confidence
            if source_session is not None:
                metadata["source_session"] = source_session
            if source_turn is not None:
                metadata["source_turn"] = source_turn
            if status is not None:
                metadata["status"] = status
            if supersedes is not None:
                metadata["supersedes"] = supersedes
            if last_confirmed_at is not None:
                metadata["last_confirmed_at"] = last_confirmed_at
            if content is None:
                content = existing_content
            elif not isinstance(content, str) or not content.strip():
                return False, "Content must not be empty"

            # Rebuild
            try:
                if metadata.get("name") != name:
                    return False, "Existing memory name does not match filename"
                mem_type = MemoryType(metadata["type"])
            except (KeyError, TypeError, ValueError):
                return False, "Invalid existing memory type"
            metadata_error = self._validate_metadata(
                description=metadata.get("description", ""),
                tags=metadata.get("tags"),
                confidence=metadata.get("confidence"),
                source_session=metadata.get("source_session"),
                source_turn=metadata.get("source_turn"),
                status=metadata.get("status", "active"),
                supersedes=metadata.get("supersedes"),
                last_confirmed_at=metadata.get("last_confirmed_at"),
            )
            if metadata_error:
                return False, metadata_error
            if mem_type == MemoryType.FEEDBACK and not self._has_feedback_structure(content):
                return False, "Feedback memory must include '**Why:**' and '**How to apply:**'"
            if mem_type == MemoryType.PROJECT and self._has_relative_dates(content):
                return (
                    False,
                    "Project memory should use absolute dates, not relative (e.g., '2024-03-15' not 'last week')",
                )

            confidence_value = self._as_float(metadata.get("confidence"), 1.0)
            if confidence_value is None:
                confidence_value = 1.0
            new_frontmatter = self._build_frontmatter(
                name=metadata["name"],
                description=metadata["description"],
                mem_type=mem_type,
                tags=metadata.get("tags"),
                created_at=self._as_float(metadata.get("created_at")),
                confidence=confidence_value,
                source_session=metadata.get("source_session"),
                source_turn=self._as_int(metadata.get("source_turn")),
                status=metadata.get("status", "active"),
                supersedes=metadata.get("supersedes"),
                last_confirmed_at=self._as_float(metadata.get("last_confirmed_at")),
            )

            full_content = f"{new_frontmatter}\n\n{content}"

            # Write
            is_valid, error = self._atomic_write_validated(
                file_path, full_content, replace_existing=True
            )
            if not is_valid:
                return False, error

            return True, "Updated successfully"

        except Exception as e:
            return False, f"Failed to update: {e}"

    def delete(self, name: str) -> tuple[bool, str]:
        """Delete a memory."""
        if not self._is_valid_name(name):
            return False, "Invalid name (use kebab-case)"
        file_path = self.memory_dir / f"{name}.md"
        if not file_path.exists():
            return False, f"Memory '{name}' not found"
        if file_path.is_symlink():
            return False, "Refusing to delete a symbolic link"

        try:
            file_path.unlink()
            return True, "Deleted successfully"
        except Exception as e:
            return False, f"Failed to delete: {e}"

    def _build_frontmatter(
        self,
        name: str,
        description: str,
        mem_type: MemoryType,
        tags: list[str] | None = None,
        created_at: float | None = None,
        confidence: float = 1.0,
        source_session: str | None = None,
        source_turn: int | None = None,
        status: str = "active",
        supersedes: str | None = None,
        last_confirmed_at: float | None = None,
    ) -> str:
        """Build frontmatter string."""
        now = time.time()
        created = created_at if created_at is not None else now

        tags_str = ", ".join(tags) if tags else ""

        safe_confidence = self._clamp_confidence(confidence)
        frontmatter = f"""---
name: {name}
description: {description}
type: {mem_type.value}
created_at: {created}
updated_at: {now}
confidence: {safe_confidence}
status: {status}
tags: {tags_str}
---"""
        optional = []
        if source_session:
            optional.append(f"source_session: {source_session}")
        if source_turn is not None:
            optional.append(f"source_turn: {source_turn}")
        if supersedes:
            optional.append(f"supersedes: {supersedes}")
        if last_confirmed_at is not None:
            optional.append(f"last_confirmed_at: {last_confirmed_at}")
        if optional:
            frontmatter = frontmatter[:-3] + "\n" + "\n".join(optional) + "\n---"
        return frontmatter

    def _parse_frontmatter_fields(self, frontmatter: str) -> dict[str, Any]:
        """Parse frontmatter into dict."""
        fields: dict[str, Any] = {}
        for line in frontmatter.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()

        # Parse tags
        if "tags" in fields:
            tags_str = str(fields["tags"]).strip().strip("[]")
            fields["tags"] = [t.strip().strip("'\"") for t in tags_str.split(",") if t.strip()]

        return fields

    @staticmethod
    def _as_float(value: Any, default: float | None = None) -> float | None:
        if value is None or value == "":
            return default
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _atomic_write_validated(
        file_path: Path,
        content: str,
        *,
        replace_existing: bool,
    ) -> tuple[bool, str]:
        """Validate a temporary file, then publish it atomically.

        Creation uses an atomic hard-link so a concurrent creator cannot be
        overwritten after the initial existence check. Updates intentionally
        replace the destination, preserving the old file if validation fails.
        """
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{file_path.stem}.", suffix=".tmp", dir=file_path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            is_valid, error = validate_memory_file(temp_path)
            if not is_valid:
                return False, error

            if replace_existing:
                temp_path.replace(file_path)
            else:
                # ``link`` fails when another process won the create race and
                # never replaces an existing destination.
                os.link(temp_path, file_path)
            return True, ""
        except Exception as exc:
            return False, f"Failed to write: {exc}"
        finally:
            if temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()

    def _is_valid_name(self, name: str) -> bool:
        """Check if name is valid kebab-case."""
        return is_valid_memory_name(name)

    @staticmethod
    def _clamp_confidence(value: Any, default: float = 1.0) -> float:
        parsed = MemoryWriter._as_float(value, default)
        if parsed is None:
            parsed = default
        return max(0.0, min(1.0, parsed))

    @staticmethod
    def _validate_metadata(
        *,
        description: Any,
        tags: Any,
        confidence: Any,
        source_session: Any,
        source_turn: Any,
        status: Any,
        supersedes: Any,
        last_confirmed_at: Any,
    ) -> str:
        """Validate all frontmatter values that are written as scalars."""
        if not isinstance(description, str) or not description.strip():
            return "Description must not be empty"
        if len(description) > 240 or any(char in description for char in "\r\n"):
            return "Description must be one line and at most 240 characters"

        if tags is not None:
            if not isinstance(tags, list) or len(tags) > 20:
                return "Tags must be a list with at most 20 entries"
            if any(
                not isinstance(tag, str)
                or not tag.strip()
                or len(tag) > 64
                or any(char in tag for char in "\r\n,")
                for tag in tags
            ):
                return "Tags must be non-empty single-line values without commas"

        if MemoryWriter._as_float(confidence, 1.0) is None:
            return "Confidence must be a finite number"
        if source_session is not None and (
            not isinstance(source_session, str)
            or len(source_session) > 200
            or any(char in source_session for char in "\r\n")
        ):
            return "Invalid source_session"
        if source_turn is not None:
            try:
                int(source_turn)
            except (TypeError, ValueError):
                return "source_turn must be an integer"
        if not isinstance(status, str) or status not in {"active", "superseded", "archived"}:
            return "Invalid memory status"
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or len(supersedes) > 80
            or not is_valid_memory_name(supersedes)
        ):
            return "Invalid supersedes name"
        if last_confirmed_at is not None and MemoryWriter._as_float(last_confirmed_at) is None:
            return "last_confirmed_at must be a finite number"
        return ""

    def _has_feedback_structure(self, content: str) -> bool:
        """Check if feedback has Why and How sections."""
        return "**Why:**" in content and "**How to apply:**" in content

    def _has_relative_dates(self, content: str) -> bool:
        """Check for relative date references."""
        relative_terms = [
            "yesterday",
            "today",
            "tomorrow",
            "last week",
            "next week",
            "last month",
            "next month",
            "recently",
            "soon",
        ]

        content_lower = content.lower()
        return any(term in content_lower for term in relative_terms)


def create_memory_template(mem_type: MemoryType) -> str:
    """Get template content for memory type."""
    templates = {
        MemoryType.USER: """# User Profile

[Describe who the user is: role, expertise level, preferences]

## Technical Background
- [Skills/experience]

## Preferences
- [Coding style, patterns, conventions]
""",
        MemoryType.FEEDBACK: """# Feedback: [Brief title]

[Describe the correction or confirmed approach]

**Why:** [Explain the reasoning or problem this addresses]

**How to apply:** [Specific guidance for future work]
""",
        MemoryType.PROJECT: """# Project: [Name]

[Overview of what's being built]

## Goals
- [Main objectives]

## Constraints
- [Technical or business constraints]

## Key Decisions
- 2024-03-15: [Decision made with rationale]
""",
        MemoryType.REFERENCE: """# Reference: [Title]

[Description of the resource]

## Links
- [URL or location]

## When to Use
- [Context where this reference is relevant]
""",
    }

    return templates.get(mem_type, "")
