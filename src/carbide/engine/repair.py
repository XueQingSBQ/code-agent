"""Repair mechanisms: orphan tool_use fixing and output truncation recovery.

Implements "ugly but working" recovery strategies that prevent conversation
state corruption when things go wrong.
"""

from __future__ import annotations

import copy
from typing import Any


class RepairManager:
    """
    Manages conversation repair mechanisms.

    Two core functions:
    1. Orphan tool_use repair: Ensure every tool_use has a tool_result
    2. Output truncation recovery: Nudge model to continue when truncated
    """

    def __init__(self, max_truncation_retries: int = 3):
        self.max_truncation_retries = max_truncation_retries
        self.truncation_count = 0

    def find_orphan_tool_uses(
        self,
        messages: list[dict[str, Any]],
    ) -> list[str]:
        """
        Find tool_use blocks without matching tool_result.

        Args:
            messages: Conversation history

        Returns:
            List of orphan tool_use IDs
        """
        tool_use_ids = set()
        tool_result_ids = set()

        for msg in messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                # Track tool_use blocks
                if block.get("type") == "tool_use":
                    tool_use_id = block.get("id")
                    if tool_use_id:
                        tool_use_ids.add(tool_use_id)

                # Track tool_result blocks
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id:
                        tool_result_ids.add(tool_use_id)

        # Find orphans (tool_use without result)
        orphans = tool_use_ids - tool_result_ids
        return list(orphans)

    def yield_missing_tool_results(
        self,
        messages: list[dict[str, Any]],
        orphan_ids: list[str],
    ) -> list[dict[str, Any]]:
        """
        Generate synthetic tool_result messages for orphans.

        This prevents API rejection due to protocol violation.
        The model sees the failure and can adjust accordingly.

        Args:
            messages: Current conversation history
            orphan_ids: List of orphaned tool_use IDs

        Returns:
            List of synthetic tool_result messages
        """
        if not orphan_ids:
            return []

        # Create synthetic results
        synthetic_results = []

        for orphan_id in orphan_ids:
            # Find the original tool_use to get tool name
            tool_name = self._find_tool_name(messages, orphan_id)

            result_msg = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": orphan_id,
                        "content": f"[Tool execution interrupted: {tool_name} did not complete. This may be due to network interruption, timeout, or process termination.]",
                        "is_error": True,
                        "_synthetic": True,
                        "_repair_reason": "orphan_tool_use",
                    }
                ],
            }

            synthetic_results.append(result_msg)

        return synthetic_results

    def _find_tool_name(
        self,
        messages: list[dict[str, Any]],
        tool_use_id: str,
    ) -> str:
        """Extract tool name from tool_use block."""
        for msg in messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id") == tool_use_id
                ):
                    return str(block.get("name", "unknown"))

        return "unknown"

    def detect_truncation(
        self,
        response: dict[str, Any],
    ) -> tuple[bool, str]:
        """
        Detect if model output was truncated.

        Args:
            response: API response message

        Returns:
            (is_truncated, stop_reason)
        """
        stop_reason = response.get("stop_reason", "")

        # Common truncation indicators
        truncation_reasons = {
            "max_tokens",
            "length",
        }

        is_truncated = stop_reason in truncation_reasons

        return is_truncated, stop_reason

    def handle_truncation(
        self,
        truncation_attempt: int,
        messages: list[dict[str, Any]],
    ) -> tuple[bool, list[dict[str, Any]] | None]:
        """
        Handle output truncation with escalating strategies.

        Three-tier escalation:
        1. First truncation: Silent retry with higher max_tokens
        2. Second truncation: Nudge model to continue
        3. Third+ truncation: Give up, return what we have

        Args:
            truncation_attempt: Current attempt number (1-indexed)
            messages: Current conversation history

        Returns:
            (should_retry, nudge_messages)
            - should_retry: True if should attempt recovery
            - nudge_messages: Messages to add for retry, or None
        """
        self.truncation_count += 1

        # Check max retries
        if truncation_attempt >= self.max_truncation_retries:
            # Give up
            return False, None

        # First truncation: silent retry (caller should increase max_tokens)
        if truncation_attempt == 1:
            return True, None

        # Second truncation: nudge model to continue
        if truncation_attempt == 2:
            nudge_msg = {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Your response was truncated. Please continue from where you left off. Focus on completing the current task concisely.",
                    }
                ],
                "_truncation_nudge": True,
            }
            return True, [nudge_msg]

        # Third+ truncation: give up
        return False, None

    def repair_conversation(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Apply all necessary repairs to conversation history.

        Args:
            messages: Conversation history to repair

        Returns:
            (repaired_messages, repair_report)
        """
        repaired = copy.deepcopy(messages)
        report: dict[str, Any] = {
            "orphans_fixed": 0,
            "repairs_applied": [],
        }

        # Fix orphan tool_uses
        orphan_ids = self.find_orphan_tool_uses(repaired)
        if orphan_ids:
            synthetic_results = self.yield_missing_tool_results(repaired, orphan_ids)
            repaired.extend(synthetic_results)
            report["orphans_fixed"] = len(orphan_ids)
            report["repairs_applied"].append("orphan_tool_use")

        return repaired, report

    def reset_truncation_count(self) -> None:
        """Reset truncation counter (e.g., after successful turn)."""
        self.truncation_count = 0

    def get_stats(self) -> dict[str, Any]:
        """Get repair statistics."""
        return {
            "truncation_count": self.truncation_count,
            "max_truncation_retries": self.max_truncation_retries,
        }


def validate_message_invariants(messages: list[dict[str, Any]]) -> list[str]:
    """
    Validate message history invariants.

    This is the core property that repair mechanisms must preserve:
    Every tool_use must have exactly one matching tool_result.

    Args:
        messages: Conversation history

    Returns:
        List of violations (empty if valid)
    """
    violations: list[str] = []

    tool_uses: dict[str, int] = {}  # tool_use_id -> count
    tool_results: dict[str, int] = {}  # tool_use_id -> count

    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            if block.get("type") == "tool_use":
                tool_use_id = block.get("id")
                if tool_use_id:
                    tool_uses[tool_use_id] = tool_uses.get(tool_use_id, 0) + 1

            if block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if tool_use_id:
                    tool_results[tool_use_id] = tool_results.get(tool_use_id, 0) + 1

    # Check for orphan tool_uses (no result)
    for tool_use_id, count in tool_uses.items():
        if tool_use_id not in tool_results:
            violations.append(f"Orphan tool_use: {tool_use_id}")
        elif tool_results[tool_use_id] != count:
            violations.append(
                f"Mismatched tool_use/tool_result count for {tool_use_id}: "
                f"{count} uses, {tool_results[tool_use_id]} results"
            )

    # Check for orphan tool_results (no use)
    for tool_use_id in tool_results:
        if tool_use_id not in tool_uses:
            violations.append(f"Orphan tool_result: {tool_use_id}")

    return violations


def find_orphan_tool_uses(messages: list[dict[str, Any]]) -> list[str]:
    """Return tool-use IDs that do not have a matching result."""

    return RepairManager().find_orphan_tool_uses(messages)


def verify_pairing(messages: list[dict[str, Any]]) -> list[str]:
    """Return violations in the tool-use/tool-result history invariant."""

    return validate_message_invariants(messages)


def repair_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Repair orphaned tool uses without duplicating result messages."""

    # Content lists are nested and must be copied too. A shallow copy would
    # mutate the caller's persisted history when a synthetic result is merged,
    # making the repair count appear to change between two observations.
    repaired = copy.deepcopy(messages)
    orphan_ids = find_orphan_tool_uses(repaired)
    if not orphan_ids:
        return repaired, 0

    synthetic = RepairManager().yield_missing_tool_results(repaired, orphan_ids)
    existing_result_message = next(
        (
            message
            for message in reversed(repaired)
            if message.get("role") == "user"
            and isinstance(message.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in message["content"]
            )
        ),
        None,
    )

    if existing_result_message is None:
        repaired.extend(synthetic)
    else:
        content = existing_result_message["content"]
        assert isinstance(content, list)
        for result_message in synthetic:
            content.append(result_message["content"][0])
        order: dict[Any, int] = {}
        for message in repaired:
            if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    order.setdefault(block.get("id"), len(order))
        content.sort(key=lambda block: order.get(block.get("tool_use_id"), len(order)))

    return repaired, len(orphan_ids)
