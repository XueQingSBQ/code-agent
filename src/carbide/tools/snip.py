"""Snip tool: Model-directed message deletion.

Allows the model to explicitly request deletion of messages by ID to manage
context window. Part of L2 compression strategy.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult


class SnipParams(BaseModel):
    """Parameters for the snip tool."""

    message_ids: list[str] = Field(
        description="List of message IDs to mark for deletion. Use the message ID from previous turns."
    )

    reason: str | None = Field(
        default=None,
        description="Optional reason for deletion (for logging/debugging)",
    )


class SnipTool(Tool):
    """
    Tool for model to request message deletion.

    The model can mark messages as unnecessary and request their removal
    to free up context window space.
    """

    name: ClassVar[str] = "snip"
    description: ClassVar[str] = "Mark messages for deletion to free context window space"
    read_only: ClassVar[bool] = True  # Doesn't modify files
    destructive: ClassVar[bool] = False  # Doesn't delete files
    concurrency_safe: ClassVar[bool] = True  # Can run in parallel
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE

    params: ClassVar[type[BaseModel]] = SnipParams

    def __init__(self, snip_compressor: Any = None) -> None:
        """
        Initialize snip tool.

        Args:
            snip_compressor: SnipCompressor instance to register deletions
        """
        super().__init__()
        self.snip_compressor = snip_compressor

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """
        Mark messages for deletion.

        Args:
            params: Snip parameters
            ctx: Execution context

        Returns:
            Tool result confirming deletion request
        """
        if not isinstance(params, SnipParams):
            return ToolResult(content="Invalid snip parameters", is_error=True)

        if not self.snip_compressor:
            return ToolResult(
                content="Snip tool not configured (no SnipCompressor available)",
                is_error=True,
            )

        if not params.message_ids:
            return ToolResult(
                content="No message IDs provided",
                is_error=True,
            )

        # Register messages for deletion
        self.snip_compressor.mark_multiple(params.message_ids)

        reason_str = f" (Reason: {params.reason})" if params.reason else ""

        return ToolResult(
            content=f"Marked {len(params.message_ids)} message(s) for deletion{reason_str}. "
            f"Deletion will be applied during next compression cycle.",
            is_error=False,
        )

    def get_pending_snips(self) -> set[str]:
        """Get currently pending snip IDs."""
        if self.snip_compressor:
            return cast(set[str], self.snip_compressor.get_pending_snips())
        return set()
