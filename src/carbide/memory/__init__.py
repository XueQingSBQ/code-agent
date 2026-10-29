"""Memory system: persistent knowledge with typed recall.

Four-type system (user/feedback/project/reference) with LLM-powered selection
and parallel prefetch for zero-latency injection.
"""

from carbide.memory.base import (
    Memory,
    MemoryIndex,
    MemoryMetadata,
    MemoryType,
    is_valid_memory_name,
    validate_memory_file,
)
from carbide.memory.extractor import (
    MemoryCandidate,
    MemoryExtractionError,
    MemoryExtractor,
    MemoryTurn,
)
from carbide.memory.manager import MemoryManager, MemoryProcessResult, MemoryWriteMode
from carbide.memory.recall import MemoryPrefetcher, MemoryRecall
from carbide.memory.worker import MemoryJob, MemoryJobResult, MemoryWorker
from carbide.memory.writer import MemoryWriter, create_memory_template

__all__ = [
    "Memory",
    "MemoryCandidate",
    "MemoryExtractionError",
    "MemoryExtractor",
    "MemoryIndex",
    "MemoryJob",
    "MemoryJobResult",
    "MemoryManager",
    "MemoryMetadata",
    "MemoryPrefetcher",
    "MemoryProcessResult",
    "MemoryRecall",
    "MemoryTurn",
    "MemoryType",
    "MemoryWorker",
    "MemoryWriteMode",
    "MemoryWriter",
    "create_memory_template",
    "is_valid_memory_name",
    "validate_memory_file",
]
