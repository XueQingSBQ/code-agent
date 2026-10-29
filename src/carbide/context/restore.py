"""L4.5 Compression: Post-compact restoration.

After L4 full compaction, immediately restore critical context that the model
is likely to re-fetch anyway. Prevents the "read everything again" reflex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RestoreResult:
    """Result of post-compact restoration."""

    restored: bool
    files_restored: int
    tokens_added: int
    skills_restored: int


class RestoreCompressor:
    """
    L4.5: Post-compact restoration.

    Strategy:
    - Trigger: Immediately after L4 compaction
    - Action: Re-inject recent files + active skills + plan
    - Budget: 5 files x 5K tokens + skills = ~50K tokens max
    - Cost: Zero API calls (read from disk/memory)

    Key insight: Without this, model's first action post-compression is to
    re-read all the files it was just working on. The token savings from L4
    get immediately spent. This layer prevents that waste.
    """

    def __init__(
        self,
        workspace: Path,
        max_files: int = 5,
        max_tokens_per_file: int = 5_000,
        max_total_tokens: int = 50_000,
    ):
        if max_files < 0:
            raise ValueError("max_files must be non-negative")
        if max_tokens_per_file < 1:
            raise ValueError("max_tokens_per_file must be positive")
        if max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive")
        self.workspace = workspace.resolve()
        self.max_files = max_files
        self.max_tokens_per_file = max_tokens_per_file
        self.max_total_tokens = max_total_tokens

    async def restore(
        self,
        messages: list[dict[str, Any]],
        recently_accessed_files: list[Path],
        active_skills: list[str] | None = None,
        current_plan: str | None = None,
    ) -> tuple[list[dict[str, Any]], RestoreResult]:
        """
        Restore critical context after L4 compaction.

        Args:
            messages: Compressed message history (post-L4)
            recently_accessed_files: Files accessed in recent turns
            active_skills: Names of skills currently in use
            current_plan: Active plan text if in plan mode

        Returns:
            (restored_messages, restore_result)
        """
        # Check if last message is L4 summary
        if not self._is_post_compact(messages):
            return messages, RestoreResult(
                restored=False,
                files_restored=0,
                tokens_added=0,
                skills_restored=0,
            )

        restored_messages = messages.copy()
        total_tokens = 0
        files_restored = 0
        skills_restored = 0

        # 1. Restore recently accessed files (most important)
        file_restoration = await self._restore_files(
            recently_accessed_files[: self.max_files], messages
        )

        for file_msg in file_restoration:
            file_tokens = len(str(file_msg)) // 4
            if total_tokens + file_tokens > self.max_total_tokens:
                break

            restored_messages.append(file_msg)
            files_restored += 1
            total_tokens += file_tokens

        # 2. Restore active skills (if any)
        if active_skills and total_tokens < self.max_total_tokens:
            skill_msg = self._restore_skills(active_skills)
            skill_tokens = len(str(skill_msg)) // 4

            if total_tokens + skill_tokens <= self.max_total_tokens:
                restored_messages.append(skill_msg)
                skills_restored = len(active_skills)
                total_tokens += skill_tokens

        # 3. Restore current plan (if any)
        if current_plan and total_tokens < self.max_total_tokens:
            plan_msg = self._restore_plan(current_plan)
            plan_tokens = len(str(plan_msg)) // 4

            if total_tokens + plan_tokens <= self.max_total_tokens:
                restored_messages.append(plan_msg)
                total_tokens += plan_tokens

        return restored_messages, RestoreResult(
            restored=True,
            files_restored=files_restored,
            tokens_added=total_tokens,
            skills_restored=skills_restored,
        )

    def _is_post_compact(self, messages: list[dict[str, Any]]) -> bool:
        """Check if messages contain L4 compaction marker."""
        # Complete recent turns can contain more than five messages. The
        # manager invokes restoration immediately after a successful compact.
        return any(msg.get("_l4_summary") for msg in reversed(messages))

    async def _restore_files(
        self,
        file_paths: list[Path],
        retained_messages: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Restore file contents as user messages.

        Args:
            file_paths: Files to restore

        Returns:
            List of messages with file contents
        """
        restored = []

        for file_path in file_paths:
            resolved = self._resolve_workspace_file(file_path)
            if resolved is None:
                continue

            try:
                # Read file with size limit
                content = resolved.read_text(encoding="utf-8")
                if self._contains_current_read(retained_messages or [], resolved, content):
                    continue

                # Truncate if too large
                max_chars = self.max_tokens_per_file * 4
                if len(content) > max_chars:
                    content = (
                        content[:max_chars]
                        + f"\n\n[... truncated, {len(content) - max_chars} chars omitted]"
                    )

                # Create restoration message
                msg = {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"[L4.5 restoration: {resolved.name}]\n\n```\n{content}\n```",
                        }
                    ],
                    "_l45_restored_file": str(resolved),
                }

                restored.append(msg)

            except (OSError, UnicodeError):
                # Skip files that can't be read
                continue

        return restored

    def _contains_current_read(
        self,
        messages: list[dict[str, Any]],
        path: Path,
        content: str,
    ) -> bool:
        """Avoid restoring bytes already kept by L4, while rejecting stale reads."""
        calls: set[str] = set()
        for message in messages:
            blocks = message.get("content", [])
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "read":
                    raw_path = block.get("input", {}).get("path")
                    if raw_path and self._resolve_workspace_file(Path(raw_path)) == path:
                        calls.add(block["id"])
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    result = block.get("content", "")
                    if isinstance(result, str):
                        plain = "\n".join(
                            re.sub(r"^\s*\d+\t", "", line) for line in result.splitlines()
                        )
                        if plain == content.rstrip("\n"):
                            return True
        return False

    def _resolve_workspace_file(self, file_path: Path) -> Path | None:
        """Resolve a candidate path and reject files outside the workspace."""
        root = self.workspace.resolve()
        candidate = file_path if file_path.is_absolute() else root / file_path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _restore_skills(self, skill_names: list[str]) -> dict[str, Any]:
        """
        Restore active skills as a user message.

        Args:
            skill_names: Names of skills to restore

        Returns:
            Message with skill restoration
        """
        skills_text = "\n".join(f"- {name}" for name in skill_names)

        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"[L4.5 restoration: Active skills]\n\n{skills_text}",
                }
            ],
            "_l45_restored_skills": skill_names,
        }

    def _restore_plan(self, plan_text: str) -> dict[str, Any]:
        """
        Restore current plan as a user message.

        Args:
            plan_text: Plan content

        Returns:
            Message with plan restoration
        """
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"[L4.5 restoration: Current plan]\n\n{plan_text}",
                }
            ],
            "_l45_restored_plan": True,
        }

    def extract_recently_accessed_files(
        self,
        messages: list[dict[str, Any]],
        lookback: int = 10,
    ) -> list[Path]:
        """
        Extract file paths from recent tool calls.

        Args:
            messages: Conversation history
            lookback: How many recent messages to examine

        Returns:
            List of file paths, most recent first
        """
        if lookback < 1:
            return []

        file_paths: list[Path] = []
        seen: set[Path] = set()

        # Scan recent messages backwards
        for msg in reversed(messages[-lookback:]):
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                # Check tool_use blocks for file operations
                if block.get("type") == "tool_use":
                    args = block.get("input", {})
                    if not isinstance(args, dict):
                        continue

                    # Extract file_path from various tool arguments
                    file_path_str = None
                    if "file_path" in args:
                        file_path_str = args["file_path"]
                    elif "path" in args:
                        file_path_str = args["path"]

                    if file_path_str:
                        try:
                            file_path = self._resolve_workspace_file(Path(file_path_str))
                            if file_path is not None and file_path not in seen:
                                file_paths.append(file_path)
                                seen.add(file_path)
                        except (OSError, ValueError):
                            continue

        return file_paths

    def get_stats(self) -> dict[str, Any]:
        """Get restoration statistics."""
        return {
            "max_files": self.max_files,
            "max_tokens_per_file": self.max_tokens_per_file,
            "max_total_tokens": self.max_total_tokens,
            "workspace": str(self.workspace),
        }
