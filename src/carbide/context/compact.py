"""L4 Compression: Full-context summarization.

Triggers when effective window drops below 13K tokens. Uses LLM to create
a 9-segment structured summary of the entire conversation history.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

SummaryFunc = Callable[[list[dict[str, Any]], str], Awaitable[str]]


@dataclass
class CompactResult:
    """Result of full compaction."""

    compacted: bool
    tokens_freed: int
    summary_tokens: int
    segments_created: int
    api_calls: int = 1  # Compaction requires one API call


class CompactCompressor:
    """
    L4: Full-context summarization.

    Strategy:
    - Trigger: Effective window < 13K tokens remaining
    - Action: LLM-powered summary with 9-segment structure
    - Cost: One API call (acceptable at this threshold)
    - Output: Structured summary preserving key context

    Nine segments:
    1. Task objective
    2. Key decisions made
    3. Files modified (paths + brief changes)
    4. Active problems/blockers
    5. Completed sub-tasks
    6. Important tool results
    7. Error patterns encountered
    8. User preferences/instructions
    9. Next steps/pending work
    """

    TRIGGER_THRESHOLD = 13_000  # Tokens remaining
    SUMMARY_TEMPLATE = """Summarize this conversation history into 9 structured segments:

1. **Task Objective**: What is the user trying to accomplish?
2. **Key Decisions**: Important technical/architectural choices made
3. **Files Modified**: List of changed files with brief description
4. **Active Problems**: Current blockers or issues needing attention
5. **Completed Sub-tasks**: What has been successfully finished
6. **Important Tool Results**: Critical information from tool outputs
7. **Error Patterns**: Recurring failures or issues encountered
8. **User Preferences**: Instructions, coding style, or preferences stated
9. **Next Steps**: Pending work or planned actions

Keep each segment concise. Focus on information the model needs to continue effectively.
Omit details that can be re-fetched (file contents, tool outputs that can be re-run).
"""

    def __init__(
        self,
        trigger_threshold: int = TRIGGER_THRESHOLD,
        max_retries: int = 3,
    ):
        self.trigger_threshold = trigger_threshold
        self.max_retries = max_retries
        self.failure_count = 0
        self._recursion_guard = False

    def should_compact(self, remaining_tokens: int) -> bool:
        """
        Check if compaction should trigger.

        Args:
            remaining_tokens: Tokens remaining in budget

        Returns:
            True if should compact
        """
        # Circuit breaker: stop after 3 consecutive failures
        if self.failure_count >= self.max_retries:
            return False

        # Recursion guard: don't compact during compaction
        if self._recursion_guard:
            return False

        return remaining_tokens < self.trigger_threshold

    async def compress(
        self,
        messages: list[dict[str, Any]],
        summarize_func: SummaryFunc,
    ) -> tuple[list[dict[str, Any]], CompactResult]:
        """
        Compress by creating a structured summary.

        Args:
            messages: Conversation history
            summarize_func: Async function to call LLM for summarization

        Returns:
            (compressed_messages, compact_result)
        """
        if len(messages) <= 5:
            # Too few messages to meaningfully compact
            return messages, CompactResult(
                compacted=False,
                tokens_freed=0,
                summary_tokens=0,
                segments_created=0,
                api_calls=0,
            )

        # Set recursion guard
        self._recursion_guard = True

        try:
            # Generate summary
            summary_text = await summarize_func(messages, self.SUMMARY_TEMPLATE)
            if not summary_text.strip():
                raise ValueError("Compaction returned an empty summary")

            # Parse into segments (basic parsing)
            segments = self._parse_summary(summary_text)

            # Build compressed message history
            compressed = self._build_compressed_history(messages, summary_text, segments)

            # Calculate tokens freed
            original_tokens = sum(len(str(msg)) for msg in messages) // 4
            summary_tokens = len(summary_text) // 4
            tokens_freed = original_tokens - sum(len(str(msg)) for msg in compressed) // 4
            if tokens_freed <= 0:
                raise ValueError("Compaction did not reduce context size")

            # Reset failure count on success
            self.failure_count = 0

            return compressed, CompactResult(
                compacted=True,
                tokens_freed=tokens_freed,
                summary_tokens=summary_tokens,
                segments_created=len(segments),
                api_calls=1,
            )

        except Exception:
            # Track failure
            self.failure_count += 1

            # Return original messages on failure
            return messages, CompactResult(
                compacted=False,
                tokens_freed=0,
                summary_tokens=0,
                segments_created=0,
                api_calls=1,
            )

        finally:
            # Clear recursion guard
            self._recursion_guard = False

    def _parse_summary(self, summary_text: str) -> dict[str, str]:
        """
        Parse summary text into 9 segments.

        Args:
            summary_text: Raw summary from LLM

        Returns:
            Dictionary of segment_name -> content
        """
        segments = {}
        current_segment = None
        current_content: list[str] = []

        lines = summary_text.split("\n")
        for line in lines:
            # Check if line is a segment header
            if line.startswith("1.") or "Task Objective" in line:
                current_segment = "task_objective"
            elif line.startswith("2.") or "Key Decisions" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "key_decisions"
                current_content = []
            elif line.startswith("3.") or "Files Modified" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "files_modified"
                current_content = []
            elif line.startswith("4.") or "Active Problems" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "active_problems"
                current_content = []
            elif line.startswith("5.") or "Completed Sub-tasks" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "completed_tasks"
                current_content = []
            elif line.startswith("6.") or "Important Tool Results" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "tool_results"
                current_content = []
            elif line.startswith("7.") or "Error Patterns" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "error_patterns"
                current_content = []
            elif line.startswith("8.") or "User Preferences" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "user_preferences"
                current_content = []
            elif line.startswith("9.") or "Next Steps" in line:
                if current_segment:
                    segments[current_segment] = "\n".join(current_content)
                current_segment = "next_steps"
                current_content = []
            else:
                # Content line
                if current_segment:
                    current_content.append(line)

        # Save last segment
        if current_segment:
            segments[current_segment] = "\n".join(current_content)

        return segments

    def _build_compressed_history(
        self,
        messages: list[dict[str, Any]],
        summary_text: str,
        segments: dict[str, str],
    ) -> list[dict[str, Any]]:
        """
        Build compressed message history.

        Strategy:
        - Keep first 2 messages (system prompt, initial context)
        - Insert summary as assistant message
        - Keep the previous complete user turn and the current turn
        """
        if len(messages) <= 5:
            return messages

        # Preserve the previous complete turn, including its latest decisions,
        # as well as the current turn. An arbitrary three-message suffix can
        # discard the last completed response just before a tool returns.
        user_turns = []
        for index, message in enumerate(messages):
            content = message.get("content", [])
            contains_result = isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            )
            restored = any(str(key).startswith("_l45_") for key in message)
            if message.get("role") == "user" and not contains_result and not restored:
                user_turns.append(index)
        last_start = len(messages) - 3
        if len(user_turns) >= 2:
            last_start = min(last_start, user_turns[-2])

        # Expand retained boundaries until every retained tool exchange is
        # complete. A fixed slice can otherwise keep a result without its call.
        first_end = 2
        positions: dict[str, list[int]] = {}
        for index, message in enumerate(messages):
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}:
                    tool_id = block.get("tool_use_id") or block.get("id")
                    if tool_id:
                        positions.setdefault(tool_id, []).append(index)
        changed = True
        while changed:
            changed = False
            for indices in positions.values():
                low, high = min(indices), max(indices)
                if low < first_end <= high:
                    first_end = high + 1
                    changed = True
                if low < last_start <= high:
                    last_start = low
                    changed = True
        if first_end >= last_start:
            return messages
        keep_first = messages[:first_end]
        keep_last = messages[last_start:]

        # Middle section to compress
        middle_count = last_start - first_end

        # Create summary message
        summary_msg = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"[Conversation history compressed via L4 full compaction]\n\n{summary_text}",
                }
            ],
            "_l4_summary": True,
            "_segments": segments,
            "_compressed_count": middle_count,
        }

        return [*keep_first, summary_msg, *keep_last]

    def reset_failure_count(self) -> None:
        """Reset failure counter (e.g., after manual intervention)."""
        self.failure_count = 0

    def get_stats(self) -> dict[str, Any]:
        """Get compaction statistics."""
        return {
            "failure_count": self.failure_count,
            "circuit_breaker_active": self.failure_count >= self.max_retries,
            "recursion_guard_active": self._recursion_guard,
            "trigger_threshold": self.trigger_threshold,
        }
