"""Permission engine for governing tool execution.

M2 governance layer: risk-based gating that decides whether a tool call requires
user approval. The engine inspects the tool's risk_level and the current mode,
then returns either immediate approval or a prompt for the user.

Three modes:
- AUTO: allow SAFE operations, prompt for WRITE and DESTRUCTIVE
- ALWAYS: allow everything without prompting
- NEVER: block everything except SAFE operations

The engine is stateless and synchronous — it makes a decision from the inputs,
it does not execute the tool or handle the user interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel

    from carbide.tools.base import Tool


class RiskLevel(StrEnum):
    """Risk classification for tool operations."""

    SAFE = "safe"  # Read-only, no side effects
    WRITE = "write"  # File modifications, reversible
    DESTRUCTIVE = "destructive"  # Deletion, shell commands, hard to reverse


class PermissionMode(StrEnum):
    """Permission mode for the engine."""

    ALWAYS_ALLOW = "always_allow"  # Full trust, no prompts (automation scenarios)
    PROMPT = "prompt"  # Human approval for destructive ops (default)
    AUDIT = "audit"  # Log all operations but don't block
    DENY = "deny"  # Reject all destructive operations

    # Legacy aliases for backward compatibility
    AUTO = "prompt"  # Maps to PROMPT
    ALWAYS = "always_allow"  # Maps to ALWAYS_ALLOW
    NEVER = "deny"  # Maps to DENY


@dataclass
class PermissionResult:
    """Result of a permission check."""

    allowed: bool
    prompt: str | None = None
    should_audit: bool = False
    # Human-readable explanation for a denial. ``prompt`` is kept separate
    # because an interactive caller may need to ask for approval, while a
    # non-interactive caller still needs a useful error message. These fields
    # are appended after the original fields to preserve positional callers.
    reason: str | None = None
    requires_approval: bool = False

    def __repr__(self) -> str:
        parts = [f"allowed={self.allowed}"]
        if self.prompt:
            parts.append(f"prompt={self.prompt!r}")
        if self.reason:
            parts.append(f"reason={self.reason!r}")
        if self.requires_approval:
            parts.append(f"requires_approval={self.requires_approval}")
        if self.should_audit:
            parts.append(f"should_audit={self.should_audit}")
        return f"PermissionResult({', '.join(parts)})"


class PermissionEngine:
    """Engine that decides whether a tool call should proceed."""

    def __init__(self, mode: PermissionMode = PermissionMode.AUTO) -> None:
        self.mode = mode

    def check(self, tool: Tool, args: BaseModel) -> PermissionResult:
        """Check if a tool call should be allowed.

        Args:
            tool: The tool being invoked
            args: The arguments for the tool call

        Returns:
            PermissionResult with allowed flag, optional prompt, and audit flag
        """
        # Normalize legacy mode names
        mode = self.mode
        if mode == PermissionMode.AUTO:
            mode = PermissionMode.PROMPT
        elif mode == PermissionMode.ALWAYS:
            mode = PermissionMode.ALWAYS_ALLOW
        elif mode == PermissionMode.NEVER:
            mode = PermissionMode.DENY

        # ALWAYS_ALLOW: allow everything, no audit
        if mode == PermissionMode.ALWAYS_ALLOW:
            return PermissionResult(allowed=True, should_audit=False)

        # SAFE operations: always allowed in all modes
        if tool.risk_level == RiskLevel.SAFE:
            # Audit mode records even safe operations
            should_audit = mode == PermissionMode.AUDIT
            return PermissionResult(allowed=True, should_audit=should_audit)

        # DENY: block all destructive operations
        if mode == PermissionMode.DENY:
            if tool.risk_level == RiskLevel.DESTRUCTIVE:
                reason = f"{tool.name} is destructive and denied by policy"
                return PermissionResult(
                    allowed=False,
                    # Keep the prompt populated for compatibility with the
                    # original public result shape. The reason is what
                    # non-interactive executors should display.
                    prompt=reason,
                    reason=reason,
                    should_audit=True,
                )
            # WRITE operations are allowed in DENY mode, but audited
            return PermissionResult(allowed=True, should_audit=True)

        # AUDIT: allow everything but log all operations
        if mode == PermissionMode.AUDIT:
            return PermissionResult(allowed=True, should_audit=True)

        # PROMPT: require approval for destructive operations
        if mode == PermissionMode.PROMPT:
            if tool.risk_level == RiskLevel.DESTRUCTIVE:
                prompt = self._generate_prompt(tool, args)
                return PermissionResult(
                    allowed=False,
                    prompt=prompt,
                    reason=prompt,
                    requires_approval=True,
                    should_audit=True,
                )
            # WRITE operations are allowed but audited
            return PermissionResult(allowed=True, should_audit=True)

        # Fallback: deny unknown modes
        return PermissionResult(allowed=False, should_audit=True)

    def _generate_prompt(self, tool: Tool, args: BaseModel) -> str:
        """Generate a human-readable prompt for approval."""
        risk_desc = {
            RiskLevel.WRITE: "write operation",
            RiskLevel.DESTRUCTIVE: "destructive operation",
        }.get(tool.risk_level, "operation")

        return f"Tool {tool.name!r} requires approval: {risk_desc}. Allow?"
