"""Context compression for managing token budgets.

M3 execution layer: compresses conversation history to stay within token limits
while preserving important context and recent messages.
"""

from __future__ import annotations

from typing import Any


class TokenBudget:
    """Manages token budget and triggers compression."""

    def __init__(self, max_tokens: int = 200_000, compression_threshold: float = 0.8):
        self.max_tokens = max_tokens
        self.compression_threshold = compression_threshold
        self.current_usage = 0

    def estimate_tokens(self, content: str | dict[str, Any] | list[Any]) -> int:
        """
        Estimate token count for content.

        Simple heuristic: ~4 characters per token for English text.
        For structured data, convert to string first.
        """
        if isinstance(content, str):
            return len(content) // 4
        else:
            # Convert to string representation
            return len(str(content)) // 4

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """Check if messages should be compressed based on token count."""
        total_tokens = sum(self.estimate_tokens(msg) for msg in messages)
        self.current_usage = total_tokens
        return total_tokens > self.max_tokens * self.compression_threshold

    def get_usage_ratio(self) -> float:
        """Get current usage as a ratio of max tokens."""
        return self.current_usage / self.max_tokens if self.max_tokens > 0 else 0.0


class MessageCompressor:
    """Compresses message history while preserving important context."""

    def __init__(
        self,
        keep_first: int = 2,
        keep_last: int = 5,
        preserve_system: bool = True,
    ):
        self.keep_first = keep_first
        self.keep_last = keep_last
        self.preserve_system = preserve_system

    def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Compress message history.

        Strategy:
        1. Keep first N messages (system messages, initial context)
        2. Keep last N messages (recent conversation)
        3. Compress middle section (summarize tool calls and results)
        """
        if len(messages) <= (self.keep_first + self.keep_last):
            return messages  # Not enough messages to compress

        # Split into sections
        first_section = messages[: self.keep_first]
        last_section = messages[-self.keep_last :]
        middle_section = messages[self.keep_first : -self.keep_last]

        # Compress middle section
        compressed_middle = self._compress_middle(middle_section)

        return first_section + compressed_middle + last_section

    def _compress_middle(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Compress middle messages.

        Strategy:
        - Count tool calls and results
        - Create a summary message
        - Preserve system messages if configured
        """
        if not messages:
            return []

        # Collect statistics
        tool_calls = 0
        tool_results = 0
        system_messages = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system" and self.preserve_system:
                system_messages.append(msg)
            elif role == "assistant":
                # Check if contains tool use
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_calls += 1
            elif role == "user":
                # Check if contains tool result
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_results += 1

        # Create summary
        summary = self._create_summary(tool_calls, tool_results)

        # Return system messages + summary
        return [*system_messages, summary]

    def _create_summary(self, tool_calls: int, tool_results: int) -> dict[str, Any]:
        """Create a summary message for compressed section."""
        summary_text = (
            f"[Conversation history compressed: {tool_calls} tool calls, {tool_results} results]"
        )

        return {
            "role": "assistant",
            "content": [{"type": "text", "text": summary_text}],
        }

    def get_compression_ratio(
        self, original: list[dict[str, Any]], compressed: list[dict[str, Any]]
    ) -> float:
        """Calculate compression ratio."""
        original_size = sum(len(str(msg)) for msg in original)
        compressed_size = sum(len(str(msg)) for msg in compressed)

        if original_size == 0:
            return 1.0

        return compressed_size / original_size


class AdaptiveCompressor:
    """Adaptive compressor that adjusts strategy based on content type."""

    def __init__(self, budget: TokenBudget):
        self.budget = budget
        self.compressor = MessageCompressor()

    def compress_if_needed(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Compress messages if needed based on token budget.

        Returns:
            (compressed_messages, was_compressed)
        """
        if not self.budget.should_compress(messages):
            return messages, False

        compressed = self.compressor.compress(messages)
        return compressed, True

    def adjust_strategy(self, usage_ratio: float) -> None:
        """Adjust compression strategy based on usage ratio."""
        if usage_ratio > 0.9:
            # Aggressive compression
            self.compressor.keep_first = 1
            self.compressor.keep_last = 3
        elif usage_ratio > 0.8:
            # Standard compression
            self.compressor.keep_first = 2
            self.compressor.keep_last = 5
        else:
            # Light compression
            self.compressor.keep_first = 3
            self.compressor.keep_last = 7
