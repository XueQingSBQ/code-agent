"""Compression manager: orchestrates five-layer pyramid + inter-layer bus.

The manager coordinates all compression layers and implements the "inter-layer
communication bus" - each layer reports tokens freed, downstream layers can
skip if enough space was already freed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carbide.context.budget import TokenBudget
from carbide.context.compact import CompactCompressor, CompactResult
from carbide.context.micro import MicroCompressor, MicroResult
from carbide.context.restore import RestoreCompressor, RestoreResult
from carbide.context.snip import SnipCompressor, SnipResult
from carbide.context.spill import SpillCompressor, SpillResult

SummaryFunc = Callable[[list[dict[str, Any]], str], Awaitable[str]]


@dataclass
class CompressionResult:
    """Complete result from compression manager."""

    compressed: bool
    layers_triggered: list[str]
    total_tokens_freed: int
    l1_result: SpillResult | None = None
    l2_result: SnipResult | None = None
    l3_result: MicroResult | None = None
    l4_result: CompactResult | None = None
    l45_result: RestoreResult | None = None
    failure: str | None = None


class LayerBus:
    """
    Inter-layer communication bus.

    Each layer reports tokens freed. Downstream layers check the bus
    to decide if they still need to run.
    """

    def __init__(self) -> None:
        self._tokens_freed: dict[str, int] = {}
        self._total_freed = 0

    def report(self, layer: str, tokens_freed: int) -> None:
        """Report tokens freed by a layer."""
        self._tokens_freed[layer] = tokens_freed
        self._total_freed += tokens_freed

    def get_total_freed(self) -> int:
        """Get total tokens freed across all layers."""
        return self._total_freed

    def get_layer_contribution(self, layer: str) -> int:
        """Get tokens freed by specific layer."""
        return self._tokens_freed.get(layer, 0)

    def should_skip_layer(self, target_tokens: int) -> bool:
        """
        Check if downstream layer can skip because enough freed already.

        Args:
            target_tokens: How many tokens needed to free

        Returns:
            True if already freed enough
        """
        return self._total_freed >= target_tokens

    def reset(self) -> None:
        """Reset the bus (called at start of each compression cycle)."""
        self._tokens_freed.clear()
        self._total_freed = 0


class CompressionManager:
    """
    Orchestrates the five-layer compression pyramid.

    Layers execute in sequence with inter-layer communication:
    L1 (spill) → L2 (snip) → L3 (micro) → L4 (compact) → L4.5 (restore)

    Each layer can bail early if upstream layers freed enough tokens.
    """

    def __init__(
        self,
        workspace: Path,
        budget: TokenBudget,
        spill_dir: Path | None = None,
    ):
        self.workspace = workspace
        self.budget = budget
        self.bus = LayerBus()

        # Initialize compressors
        if spill_dir is None:
            spill_dir = workspace / ".carbide" / "spill"

        self.l1_spill = SpillCompressor(spill_dir=spill_dir)
        self.l2_snip = SnipCompressor()
        self.l3_micro = MicroCompressor()
        self.l4_compact = CompactCompressor()
        self.l45_restore = RestoreCompressor(workspace=workspace)

    async def compress(
        self,
        messages: list[dict[str, Any]],
        summarize_func: SummaryFunc | None = None,
        recently_accessed_files: list[Path] | None = None,  # For L4.5
    ) -> tuple[list[dict[str, Any]], CompressionResult]:
        """
        Run compression pyramid with inter-layer communication.

        Args:
            messages: Conversation history
            summarize_func: Async function for L4 summarization
            recently_accessed_files: Files for L4.5 restoration

        Returns:
            (compressed_messages, result)
        """
        # Reset bus for new cycle
        self.bus.reset()

        layers_triggered: list[str] = []
        current_messages = messages

        # Calculate how many tokens we need to free
        current_tokens = sum(self.budget.estimate_tokens(msg).tokens for msg in messages)
        self.budget.current_usage = current_tokens
        target_tokens = max(0, current_tokens - int(self.budget.max_tokens * 0.7))

        if (
            target_tokens == 0
            and not self.l2_snip.has_pending_snips()
            and not self.l3_micro.should_compact()
        ):
            return messages, CompressionResult(
                compressed=False,
                layers_triggered=[],
                total_tokens_freed=0,
            )

        # L1: Spill large results to disk
        current_messages, l1_result = self.l1_spill.compress(current_messages)
        self._update_usage(current_messages)
        if l1_result.spilled:
            layers_triggered.append("L1:spill")
            self.bus.report("L1", l1_result.tokens_freed)

        # Check if can skip remaining layers
        if self.bus.should_skip_layer(target_tokens) and not (
            self.l2_snip.has_pending_snips() or self.l3_micro.should_compact()
        ):
            return current_messages, CompressionResult(
                compressed=True,
                layers_triggered=layers_triggered,
                total_tokens_freed=self.bus.get_total_freed(),
                l1_result=l1_result,
            )

        # L2: Apply snip deletions (if any pending)
        current_messages, l2_result = self.l2_snip.compress(current_messages)
        self._update_usage(current_messages)
        if l2_result.snipped:
            layers_triggered.append("L2:snip")
            self.bus.report("L2", l2_result.tokens_freed)

        if self.bus.should_skip_layer(target_tokens) and not self.l3_micro.should_compact():
            return current_messages, CompressionResult(
                compressed=True,
                layers_triggered=layers_triggered,
                total_tokens_freed=self.bus.get_total_freed(),
                l1_result=l1_result,
                l2_result=l2_result,
            )

        # L3: Time-based micro-compaction
        current_messages, l3_result = self.l3_micro.compress(current_messages)
        self._update_usage(current_messages)
        if l3_result.compacted:
            layers_triggered.append("L3:micro")
            self.bus.report("L3", l3_result.tokens_freed)

        if self.bus.should_skip_layer(target_tokens):
            return current_messages, CompressionResult(
                compressed=True,
                layers_triggered=layers_triggered,
                total_tokens_freed=self.bus.get_total_freed(),
                l1_result=l1_result,
                l2_result=l2_result,
                l3_result=l3_result,
            )

        # L4: Full compaction (expensive, last resort)
        remaining = self.budget.get_remaining_tokens()
        l4_result = None
        l45_result = None
        failure: str | None = None

        if self.l4_compact.should_compact(remaining):
            if summarize_func is None:
                failure = "L4 summarizer is not configured"
            else:
                current_messages, l4_result = await self.l4_compact.compress(
                    current_messages, summarize_func
                )
                if not l4_result.compacted:
                    failure = "L4 compaction failed"

                if l4_result.compacted:
                    layers_triggered.append("L4:compact")
                    self.bus.report("L4", l4_result.tokens_freed)

                    # L4.5: Restore critical context after compaction
                    if recently_accessed_files is None:
                        recently_accessed_files = self.l45_restore.extract_recently_accessed_files(
                            messages
                        )

                    current_messages, l45_result = await self.l45_restore.restore(
                        current_messages,
                        recently_accessed_files,
                    )

                    if l45_result is not None and l45_result.tokens_added > 0:
                        layers_triggered.append("L4.5:restore")
                        # L4.5 adds tokens back (negative freed)
                        self.bus.report("L4.5", -l45_result.tokens_added)

        self._update_usage(current_messages)

        return current_messages, CompressionResult(
            compressed=len(layers_triggered) > 0,
            layers_triggered=layers_triggered,
            total_tokens_freed=self.bus.get_total_freed(),
            l1_result=l1_result,
            l2_result=l2_result,
            l3_result=l3_result,
            l4_result=l4_result,
            l45_result=l45_result,
            failure=failure,
        )

    def _update_usage(self, messages: list[dict[str, Any]]) -> None:
        self.budget.current_usage = sum(self.budget.estimate_tokens(msg).tokens for msg in messages)

    def mark_snip(self, message_id: str) -> None:
        """Mark message for L2 snip deletion."""
        self.l2_snip.mark_for_snip(message_id)

    def update_api_call_time(self) -> None:
        """Update L3 micro timer (call after each API request)."""
        self.l3_micro.update_call_time()

    def get_stats(self) -> dict[str, Any]:
        """Get statistics from all layers."""
        return {
            "budget": {
                "usage_ratio": self.budget.get_usage_ratio(),
                "remaining_tokens": self.budget.get_remaining_tokens(),
                "calibration": self.budget.get_calibration_stats(),
            },
            "l1_spill": self.l1_spill.get_stats(),
            "l2_snip": {
                "pending_snips": len(self.l2_snip.get_pending_snips()),
            },
            "l3_micro": self.l3_micro.get_stats(),
            "l4_compact": self.l4_compact.get_stats(),
            "l45_restore": self.l45_restore.get_stats(),
            "bus": {
                "total_freed": self.bus.get_total_freed(),
                "by_layer": {
                    layer: self.bus.get_layer_contribution(layer)
                    for layer in ["L1", "L2", "L3", "L4", "L4.5"]
                },
            },
        }
