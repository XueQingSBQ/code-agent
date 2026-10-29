"""L2 Compression: Model-directed snip (self-pruning).

The model can use the 'snip' tool to mark messages for deletion by ID.
This compressor processes those deletion requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SnipResult:
    """Result of applying snip deletions."""

    snipped: bool
    messages_removed: int
    tokens_freed: int


class SnipCompressor:
    """
    L2: Model-directed message deletion.

    Strategy:
    - Expose 'snip' tool to model
    - Model marks messages by ID for deletion
    - This compressor removes marked messages
    - Cost: Zero API calls (model decides)
    """

    def __init__(self) -> None:
        # Track messages marked for deletion
        self._snip_ids: set[str] = set()

    def mark_for_snip(self, message_id: str) -> None:
        """
        Mark a message ID for deletion.

        Called by the snip tool when model requests deletion.
        """
        self._snip_ids.add(message_id)

    def mark_multiple(self, message_ids: list[str]) -> None:
        """Mark multiple messages for deletion."""
        self._snip_ids.update(message_ids)

    def compress(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], SnipResult]:
        """
        Compress messages by removing snipped ones.

        Returns:
            (compressed_messages, snip_result)
        """
        if not self._snip_ids:
            return messages, SnipResult(
                snipped=False,
                messages_removed=0,
                tokens_freed=0,
            )

        # Removing one side of a tool exchange invalidates the next request.
        # Expand message-level marks to the IDs of both halves, then prune the
        # matching blocks together, preserving unrelated text in those messages.
        tool_ids: set[str] = set()
        for msg in messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            marked = (msg.get("id") or msg.get("_id")) in self._snip_ids
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in {"tool_use", "tool_result"}:
                    continue
                tool_id = block.get("tool_use_id") or block.get("id")
                if tool_id and (marked or tool_id in self._snip_ids):
                    tool_ids.add(tool_id)

        compressed = []
        removed_count = 0
        tokens_freed = 0

        for msg in messages:
            msg_id = msg.get("id") or msg.get("_id")

            if msg_id and msg_id in self._snip_ids:
                # Message marked for deletion
                removed_count += 1
                tokens_freed += len(str(msg)) // 4  # Approximate tokens
                continue

            # Check if any content blocks are marked
            content = msg.get("content", [])
            if isinstance(content, list):
                new_content = []
                block_removed = False

                for block in content:
                    if isinstance(block, dict):
                        block_id = block.get("id") or block.get("tool_use_id")
                        if block_id and (block_id in self._snip_ids or block_id in tool_ids):
                            tokens_freed += len(str(block)) // 4
                            block_removed = True
                            continue

                    new_content.append(block)

                if block_removed:
                    if new_content:
                        # Keep message with remaining content
                        compressed.append({**msg, "content": new_content})
                    else:
                        # Empty message, remove entirely
                        removed_count += 1
                    continue

            # Keep message unchanged
            compressed.append(msg)

        self.clear_snip_marks()
        return compressed, SnipResult(
            snipped=bool(tokens_freed or removed_count),
            messages_removed=removed_count,
            tokens_freed=tokens_freed,
        )

    def clear_snip_marks(self) -> None:
        """Clear all snip marks (after compression applied)."""
        self._snip_ids.clear()

    def get_pending_snips(self) -> set[str]:
        """Get IDs currently marked for snipping."""
        return self._snip_ids.copy()

    def has_pending_snips(self) -> bool:
        """Check if there are pending snip requests."""
        return len(self._snip_ids) > 0


class SnipStrategy:
    """
    Helper for intelligent snip suggestions.

    This can analyze conversation history and suggest what to snip,
    but the final decision is always the model's.
    """

    @staticmethod
    def suggest_old_tool_results(
        messages: list[dict[str, Any]],
        keep_recent: int = 10,
    ) -> list[str]:
        """
        Suggest old tool results for snipping.

        Args:
            messages: Conversation history
            keep_recent: Keep this many recent results

        Returns:
            List of message IDs that could be snipped
        """
        tool_result_ids = []

        for msg in messages:
            msg_id = msg.get("id") or msg.get("_id")
            if not msg_id:
                continue

            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_result_ids.append(msg_id)
                        break

        # Keep recent ones, suggest older ones
        if len(tool_result_ids) > keep_recent:
            return tool_result_ids[:-keep_recent]

        return []

    @staticmethod
    def suggest_redundant_reads(
        messages: list[dict[str, Any]],
    ) -> list[str]:
        """
        Suggest redundant file reads for snipping.

        If the same file was read multiple times, older reads can be snipped.
        """
        file_reads: dict[str, list[str]] = {}  # file_path -> [message_ids]

        for msg in messages:
            msg_id = msg.get("id") or msg.get("_id")
            if not msg_id:
                continue

            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        # Check if it's a Read tool result
                        result_content = block.get("content", "")

                        # Heuristic: look for file path markers
                        if "file_path" in str(result_content) or msg.get("role") == "user":
                            # Track this as a potential file read
                            # Extract file path if possible
                            file_path = "unknown"  # Simplified
                            if file_path not in file_reads:
                                file_reads[file_path] = []
                            file_reads[file_path].append(msg_id)

        # Suggest older reads if file was read multiple times
        suggestions = []
        for _file_path, msg_ids in file_reads.items():
            if len(msg_ids) > 1:
                # Keep only the most recent read
                suggestions.extend(msg_ids[:-1])

        return suggestions
