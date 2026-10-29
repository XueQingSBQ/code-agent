"""Context management layer - five-layer compression pyramid.

M3 execution layer: intelligent context window management with five progressive
compression strategies and inter-layer communication bus.
"""

from carbide.context.budget import TokenBudget, TokenCalibrator
from carbide.context.compact import CompactCompressor
from carbide.context.manager import CompressionManager, CompressionResult, LayerBus
from carbide.context.micro import MicroCompressor
from carbide.context.restore import RestoreCompressor
from carbide.context.snip import SnipCompressor
from carbide.context.spill import SpillCompressor

__all__ = [
    "CompactCompressor",
    "CompressionManager",
    "CompressionResult",
    "LayerBus",
    "MicroCompressor",
    "RestoreCompressor",
    "SnipCompressor",
    "SpillCompressor",
    "TokenBudget",
    "TokenCalibrator",
]
