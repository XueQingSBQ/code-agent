"""L1 Compression: Spill large tool results to disk.

Triggers when single tool result exceeds 50KB. Writes full content to disk,
replaces in-context message with ~2KB preview + file path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SpillResult:
    """Result of spilling a large message to disk."""

    spilled: bool
    tokens_freed: int
    spill_path: Path | None = None
    preview_size: int = 0


class SpillCompressor:
    """
    L1: Spill large tool results to disk.

    Strategy:
    - Trigger: Single tool result > 50KB (or message-level total > 200KB)
    - Action: Write full content to .carbide/spill/<hash>.json
    - Replace: Leave 2KB preview + path reference
    - Cost: Zero API calls
    """

    def __init__(
        self,
        spill_dir: Path,
        single_result_threshold: int = 50 * 1024,  # 50KB
        message_total_threshold: int = 200 * 1024,  # 200KB
        preview_size: int = 2 * 1024,  # 2KB preview
    ):
        if single_result_threshold < 1:
            raise ValueError("single_result_threshold must be positive")
        if message_total_threshold < 1:
            raise ValueError("message_total_threshold must be positive")
        if preview_size < 1:
            raise ValueError("preview_size must be positive")
        self.spill_dir = spill_dir
        self.single_result_threshold = single_result_threshold
        self.message_total_threshold = message_total_threshold
        self.preview_size = preview_size

        # Ensure spill directory exists
        self.spill_dir.mkdir(parents=True, exist_ok=True)

    def compress(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], SpillResult]:
        """
        Compress messages by spilling large results to disk.

        Returns:
            (compressed_messages, spill_result)
        """
        compressed = []
        total_freed = 0
        spilled_any = False
        last_spill_path = None

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", [])

            # Only process user messages with tool_result
            if role != "user" or not isinstance(content, list):
                compressed.append(msg)
                continue

            # Check each content block
            new_content = []
            msg_changed = False

            result_blocks = [
                (index, block, len(str(block.get("content", ""))))
                for index, block in enumerate(content)
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            total_result_size = sum(size for _, _, size in result_blocks)
            spill_indices = {
                index for index, _, size in result_blocks if size > self.single_result_threshold
            }
            # A message can be oversized even when every individual result is
            # below the single-result threshold. Spill the largest blocks until
            # the aggregate tool-result payload is within the message budget.
            if total_result_size > self.message_total_threshold:
                remaining_size = total_result_size
                for index, _, size in sorted(result_blocks, key=lambda item: item[2], reverse=True):
                    if remaining_size <= self.message_total_threshold:
                        break
                    spill_indices.add(index)
                    remaining_size -= size

            for index, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    new_content.append(block)
                    continue

                # Extract tool result content
                result_content = block.get("content", "")
                result_size = len(str(result_content))

                # Check if should spill
                if index in spill_indices:
                    # Spill to disk
                    spill_path, preview = self._spill_to_disk(block)

                    # Replace with preview + reference
                    new_block = {
                        **block,
                        "content": preview,
                        "_spilled": True,
                        "_spill_path": str(spill_path),
                        "_original_size": result_size,
                    }
                    new_content.append(new_block)

                    tokens_freed = (result_size - len(preview)) // 4  # Convert to tokens
                    total_freed += tokens_freed
                    spilled_any = True
                    last_spill_path = spill_path
                    msg_changed = True
                else:
                    new_content.append(block)

            # Update message if changed
            if msg_changed:
                compressed.append({**msg, "content": new_content})
            else:
                compressed.append(msg)

        return compressed, SpillResult(
            spilled=spilled_any,
            tokens_freed=total_freed,  # Already converted to tokens above
            spill_path=last_spill_path,
            preview_size=self.preview_size,
        )

    def _spill_to_disk(self, tool_result_block: dict[str, Any]) -> tuple[Path, str]:
        """
        Write tool result to disk and return (path, preview).

        Args:
            tool_result_block: The tool_result content block

        Returns:
            (spill_file_path, preview_string)
        """
        # Generate unique filename based on content hash
        content = tool_result_block.get("content", "")
        content_hash = hashlib.sha256(str(content).encode()).hexdigest()[:12]
        tool_use_id = self._safe_filename_component(tool_result_block.get("tool_use_id", "unknown"))

        filename = f"{tool_use_id}_{content_hash}.json"
        spill_path = self.spill_dir / filename

        # Write full content to disk
        with spill_path.open("w", encoding="utf-8") as f:
            json.dump(tool_result_block, f, indent=2, ensure_ascii=False)

        # Create preview (first N bytes)
        content_str = str(content)
        if len(content_str) > self.preview_size:
            preview = (
                content_str[: self.preview_size]
                + f"\n\n[... {len(content_str) - self.preview_size} more bytes spilled to {spill_path.name}]"
            )
        else:
            preview = content_str

        return spill_path, preview

    @staticmethod
    def _safe_filename_component(value: Any) -> str:
        """Keep model-controlled IDs from escaping the spill directory."""
        component = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))
        component = component.lstrip(".")
        return component[:80] or "unknown"

    def restore_from_disk(self, spill_path: Path) -> dict[str, Any] | None:
        """
        Restore spilled content from disk.

        Returns:
            Original tool_result block, or None if file not found
        """
        if not spill_path.exists():
            return None

        try:
            with spill_path.open(encoding="utf-8") as f:
                restored = json.load(f)
            return restored if isinstance(restored, dict) else None
        except Exception:
            return None

    def cleanup_spilled_files(self, older_than_seconds: float = 86400) -> int:
        """
        Clean up spilled files older than threshold (default: 24 hours).

        Returns:
            Number of files deleted
        """
        import time

        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")

        deleted = 0
        now = time.time()

        for spill_file in self.spill_dir.glob("*.json"):
            try:
                if now - spill_file.stat().st_mtime > older_than_seconds:
                    spill_file.unlink()
                    deleted += 1
            except OSError:
                # Cleanup is best effort; one locked file must not prevent the
                # rest of the spill directory from being reclaimed.
                continue

        return deleted

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about spilled files."""
        spill_files = list(self.spill_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in spill_files)

        return {
            "spill_count": len(spill_files),
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "spill_dir": str(self.spill_dir),
        }
