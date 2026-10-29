"""L3 Compression: Time-based micro-compaction.

Clears re-fetchable tool results after 60 minutes (cache-aware threshold).
Preserves non-reproducible outputs (Agent/Task) and keeps recent 5 results.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass
class MicroResult:
    """Result of micro-compaction."""

    compacted: bool
    messages_cleared: int
    tokens_freed: int
    preserved_count: int


class MicroCompressor:
    """
    L3: Time-based micro-compaction.

    Strategy:
    - Trigger: > 60 minutes since last API call (cache likely expired)
    - Action: Clear re-fetchable tool results (Read/Bash/Grep/Glob/Edit/Write)
    - Preserve: Non-reproducible (Agent/Task) + recent 5 results
    - Cost: Zero API calls

    Key insight: 60min aligns with prompt cache TTL, so clearing doesn't
    waste cache investment. This is "cache-aware context management".
    """

    # Tools whose results can be re-fetched
    REFETCHABLE_TOOLS: ClassVar[set[str]] = {
        "read",
        "bash",
        "grep",
        "glob",
        "edit",
        "write",
        "web_search",
    }

    # Tools whose outputs are non-reproducible (never clear)
    NON_REPRODUCIBLE_TOOLS: ClassVar[set[str]] = {
        "agent",
        "task",
        "explore",
    }

    def __init__(
        self,
        time_threshold_seconds: int = 60 * 60,  # 60 minutes
        keep_recent: int = 5,
    ):
        self.time_threshold = time_threshold_seconds
        self.keep_recent = keep_recent
        self.last_call_time: float | None = None

    def update_call_time(self) -> None:
        """Update the last API call timestamp."""
        self.last_call_time = time.time()

    def should_compact(self) -> bool:
        """Check if compaction should trigger based on time."""
        if self.last_call_time is None:
            return False

        elapsed = time.time() - self.last_call_time
        return elapsed > self.time_threshold

    def compress(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], MicroResult]:
        """
        Compress by clearing old re-fetchable tool results.

        Returns:
            (compressed_messages, micro_result)
        """
        if not self.should_compact():
            return messages, MicroResult(
                compacted=False,
                messages_cleared=0,
                tokens_freed=0,
                preserved_count=0,
            )

        # Update timer to current time for next check
        self.last_call_time = time.time()

        # A parallel batch can contain both reads and non-reproducible Agent
        # results. Classify each block rather than using the first tool name
        # for the whole message.
        tool_names = {
            block["id"]: str(block.get("name", "unknown")).lower()
            for message in messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }
        tool_results = [
            (i, j)
            for i, msg in enumerate(messages)
            if self._is_tool_result_message(msg)
            for j, block in enumerate(msg["content"])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        recent_indices = set(tool_results[-self.keep_recent :]) if self.keep_recent else set()

        # Compact messages
        compressed = []
        messages_cleared = 0
        tokens_freed = 0
        preserved_count = 0

        for i, msg in enumerate(messages):
            # Not a tool result message - keep as is
            if not self._is_tool_result_message(msg):
                compressed.append(msg)
                continue

            new_content = []
            changed = False
            for j, block in enumerate(msg["content"]):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    new_content.append(block)
                    continue
                tool_name = tool_names.get(
                    block.get("tool_use_id"), self._extract_tool_name({"content": [block]})
                )
                if (
                    (i, j) in recent_indices
                    or tool_name not in self.REFETCHABLE_TOOLS
                    or tool_name in self.NON_REPRODUCIBLE_TOOLS
                    or block.get("_micro_cleared")
                ):
                    new_content.append(block)
                    preserved_count += 1
                    continue
                cleared_block = {
                    **block,
                    "content": f"[Tool result cleared by L3 micro-compaction: {tool_name} output is re-fetchable]",
                    "_micro_cleared": True,
                    "_original_size": len(str(block.get("content", ""))),
                }
                new_content.append(cleared_block)
                tokens_freed += (len(str(block)) - len(str(cleared_block))) // 4
                changed = True
            messages_cleared += int(changed)
            compressed.append({**msg, "content": new_content} if changed else msg)

        # Reset timer after compaction
        self.last_call_time = time.time()

        return compressed, MicroResult(
            compacted=messages_cleared > 0,
            messages_cleared=messages_cleared,
            tokens_freed=tokens_freed,
            preserved_count=preserved_count,
        )

    def _is_tool_result_message(self, msg: dict[str, Any]) -> bool:
        """Check if message contains tool results."""
        if msg.get("role") != "user":
            return False

        content = msg.get("content", [])
        if not isinstance(content, list):
            return False

        return any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )

    def _extract_tool_name(self, msg: dict[str, Any]) -> str:
        """Extract tool name from tool result message."""
        content = msg.get("content", [])
        if not isinstance(content, list):
            return "unknown"

        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # Check if metadata exists first (most reliable)
                if "_tool_name" in block:
                    return str(block["_tool_name"]).lower()

                # Try to extract from tool_use_id
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id:
                    # Tool use ID often contains tool name
                    # Format: "toolu_xxx" or custom format like "read_123"
                    parts = tool_use_id.split("_")
                    if len(parts) > 1:
                        return str(parts[0]).lower()

        return "unknown"

    def get_stats(self) -> dict[str, Any]:
        """Get compaction statistics."""
        if self.last_call_time is None:
            return {
                "last_call_ago_seconds": None,
                "should_compact": False,
            }

        elapsed = time.time() - self.last_call_time
        return {
            "last_call_ago_seconds": elapsed,
            "should_compact": elapsed > self.time_threshold,
            "threshold_seconds": self.time_threshold,
            "keep_recent": self.keep_recent,
        }
