"""Durable-memory lifecycle orchestration.

This module is the boundary between model-generated candidates and the
filesystem-backed memory store.  Extraction failures are intentionally
non-fatal to the main agent query.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from carbide.memory.extractor import MemoryCandidate, MemoryExtractor, MemoryTurn
from carbide.memory.recall import MemoryRecall
from carbide.memory.writer import MemoryWriter


class MemoryWriteMode(StrEnum):
    """Policy for turning extracted candidates into persistent files."""

    OFF = "off"
    EXPLICIT = "explicit"
    SUGGEST = "suggest"
    AUTO = "auto"


class MemoryExtractorProtocol(Protocol):
    """Minimal extractor seam used by the manager and test/custom adapters."""

    def should_extract(self, user_message: str) -> bool:
        """Return whether a turn should invoke extraction."""
        ...

    async def extract(self, turn: MemoryTurn) -> list[MemoryCandidate]:
        """Extract validated candidates from a completed turn."""
        ...


MemoryAuditHook = Callable[[str, dict[str, Any]], None]


@dataclass
class MemoryProcessResult:
    """Observable result of processing one completed turn."""

    attempted: bool = False
    candidates: list[MemoryCandidate] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    suggested: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "candidates": [candidate.name for candidate in self.candidates],
            "saved": self.saved,
            "updated": self.updated,
            "suggested": self.suggested,
            "skipped": self.skipped,
            "errors": self.errors,
        }


class MemoryManager:
    """Coordinate extraction, policy, persistence, and index refresh."""

    MAX_PENDING_SUGGESTIONS = 100

    _SENSITIVE_RE = re.compile(
        r"(?:api[_ -]?key|password|passwd|secret|access[_ -]?token)\s*[:=]\s*\S+|"
        r"(?:authorization\s*:\s*bearer\s+\S+|sk-[A-Za-z0-9_-]{16,}|"
        r"AKIA[0-9A-Z]{16}|-----BEGIN [^-]+ PRIVATE KEY-----)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        memory_dir: Path,
        provider: Any,
        *,
        mode: MemoryWriteMode | str = MemoryWriteMode.AUTO,
        min_confidence: float = 0.85,
        extraction_timeout: float = 30.0,
        recall: MemoryRecall | None = None,
        extractor: MemoryExtractorProtocol | None = None,
        writer: MemoryWriter | None = None,
        audit_hook: MemoryAuditHook | None = None,
    ) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.mode = MemoryWriteMode(mode)
        if not math.isfinite(min_confidence) or not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if not math.isfinite(extraction_timeout) or extraction_timeout <= 0:
            raise ValueError("extraction_timeout must be positive")
        self.min_confidence = min_confidence
        self.extraction_timeout = extraction_timeout
        self.recall = recall or MemoryRecall(memory_dir)
        self.recall.scan()
        self.extractor: MemoryExtractorProtocol = extractor or MemoryExtractor(provider)
        self.writer = writer or MemoryWriter(memory_dir)
        self.audit_hook = audit_hook
        self._write_lock = asyncio.Lock()
        self._pending_suggestions: dict[str, tuple[MemoryCandidate, MemoryTurn]] = {}
        self._stats = {"saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    def should_process(self, user_message: str) -> bool:
        """Return whether this turn merits an extraction model call."""
        return self.mode is not MemoryWriteMode.OFF and self.extractor.should_extract(user_message)

    async def process_turn(self, turn: MemoryTurn) -> MemoryProcessResult:
        """Process one completed turn without allowing memory to break it."""
        result = MemoryProcessResult()
        if self.mode is MemoryWriteMode.OFF or not self.extractor.should_extract(turn.user_message):
            return result

        result.attempted = True
        self._audit("memory_extraction_started", turn)
        try:
            candidates = await asyncio.wait_for(
                self.extractor.extract(turn), timeout=self.extraction_timeout
            )
        except Exception as exc:
            result.errors.append(str(exc) or type(exc).__name__)
            self._stats["errors"] += 1
            self._audit(
                "memory_extraction_failed",
                turn,
                detail=str(exc) or type(exc).__name__,
            )
            return result

        result.candidates = candidates
        if not candidates:
            self._audit("memory_extraction_empty", turn)
            return result

        if self.mode is MemoryWriteMode.SUGGEST:
            for candidate in candidates:
                if self._is_sensitive(candidate):
                    result.skipped.append(f"{candidate.name}: sensitive-content")
                    self._stats["skipped"] += 1
                    self._audit("memory_skipped", turn, candidate, "sensitive-content")
                else:
                    result.suggested.append(candidate.name)
                    self._pending_suggestions[candidate.name] = (candidate, turn)
                    self._audit("memory_suggested", turn, candidate)
                    if len(self._pending_suggestions) > self.MAX_PENDING_SUGGESTIONS:
                        oldest_name = next(iter(self._pending_suggestions))
                        del self._pending_suggestions[oldest_name]
            return result

        persisted = await self._persist_candidates(candidates, turn, confirmed=False)
        result.saved = persisted.saved
        result.updated = persisted.updated
        result.skipped = persisted.skipped
        result.errors = persisted.errors
        return result

    async def persist_candidates(
        self,
        candidates: list[MemoryCandidate],
        turn: MemoryTurn,
    ) -> MemoryProcessResult:
        """Persist candidates after an explicit user confirmation.

        This is the confirmation seam for ``suggest`` mode and for custom UIs.
        It still applies the sensitive-content guard and writer validation, but
        the confirmation itself is treated as an explicit save decision and
        therefore permits a candidate below the automatic confidence cutoff.
        """
        result = MemoryProcessResult(attempted=True, candidates=list(candidates))
        if self.mode is MemoryWriteMode.OFF:
            result.skipped = [candidate.name for candidate in candidates]
            return result
        return await self._persist_candidates(candidates, turn, confirmed=True)

    async def persist_suggestions(self, names: list[str]) -> MemoryProcessResult:
        """Persist pending suggestions selected by name after UI confirmation.

        ``process_turn`` stores only a bounded number of pending candidates, so
        a CLI or other event consumer can confirm the names from
        ``MemoryProcessResult.to_dict()`` without receiving model-generated
        content in a serialized event payload.
        """
        result = MemoryProcessResult(attempted=True)
        for name in names:
            pending = self._pending_suggestions.pop(name, None)
            if pending is None:
                result.skipped.append(name)
                continue
            candidate, turn = pending
            persisted = await self.persist_candidates([candidate], turn)
            result.candidates.extend(persisted.candidates)
            result.saved.extend(persisted.saved)
            result.updated.extend(persisted.updated)
            result.skipped.extend(persisted.skipped)
            result.errors.extend(persisted.errors)
        return result

    async def _persist_candidates(
        self,
        candidates: list[MemoryCandidate],
        turn: MemoryTurn,
        *,
        confirmed: bool,
    ) -> MemoryProcessResult:
        """Persist a candidate batch under the manager's write lock."""
        result = MemoryProcessResult(attempted=True, candidates=list(candidates))
        async with self._write_lock:
            for candidate in candidates:
                explicit = candidate.explicit or turn.explicit_request
                if self._is_sensitive(candidate):
                    result.skipped.append(f"{candidate.name}: sensitive-content")
                    self._stats["skipped"] += 1
                    self._audit("memory_skipped", turn, candidate, "sensitive-content")
                    continue
                if not confirmed and not self._allowed(candidate, explicit):
                    result.skipped.append(candidate.name)
                    self._stats["skipped"] += 1
                    self._audit("memory_skipped", turn, candidate, "below-policy-threshold")
                    continue

                try:
                    action, detail = self._persist(candidate, turn)
                except Exception as exc:
                    action, detail = "error", str(exc)

                if action == "saved":
                    result.saved.append(candidate.name)
                    self._stats["saved"] += 1
                    self._audit("memory_saved", turn, candidate)
                elif action == "updated":
                    result.updated.append(candidate.name)
                    self._stats["updated"] += 1
                    self._audit("memory_updated", turn, candidate)
                elif action == "skipped":
                    result.skipped.append(candidate.name)
                    self._stats["skipped"] += 1
                    self._audit("memory_skipped", turn, candidate, detail)
                else:
                    result.errors.append(detail)
                    self._stats["errors"] += 1
                    self._audit("memory_persistence_failed", turn, candidate, detail)

            if result.saved or result.updated:
                # A fresh scan makes newly created and deleted files visible to
                # the next recall without restarting the Agent.
                try:
                    self.recall.scan()
                except Exception as exc:
                    # The file is already durably published; an index refresh
                    # failure is observable but must not fail the main turn.
                    result.errors.append(str(exc) or type(exc).__name__)
                    self._stats["errors"] += 1

        return result

    def _allowed(self, candidate: MemoryCandidate, explicit: bool) -> bool:
        if self.mode is MemoryWriteMode.EXPLICIT:
            return explicit
        if self.mode is MemoryWriteMode.SUGGEST:
            return True
        return explicit or candidate.confidence >= self.min_confidence

    def _persist(self, candidate: MemoryCandidate, turn: MemoryTurn) -> tuple[str, str]:
        now = time.time()
        # Another process or UI may have edited the directory since the last
        # turn. Refresh before deciding between create and update.
        self.recall.scan()
        existing = self.recall.index.get(candidate.name)
        path = self.memory_dir / f"{candidate.name}.md"

        if existing is None or not path.exists():
            success, message = self.writer.create(
                name=candidate.name,
                description=candidate.description,
                content=candidate.content,
                mem_type=candidate.mem_type,
                tags=candidate.tags,
                confidence=candidate.confidence,
                source_session=turn.session_id,
                source_turn=turn.turn_id,
                supersedes=candidate.supersedes,
                last_confirmed_at=now,
            )
            return ("saved", message) if success else ("error", message)

        old_content = self._read_body(path)
        if self._normalise(old_content) == self._normalise(candidate.content):
            return "skipped", "duplicate memory content"

        success, message = self.writer.update(
            name=candidate.name,
            content=candidate.content,
            description=candidate.description,
            tags=candidate.tags,
            confidence=candidate.confidence,
            source_session=turn.session_id,
            source_turn=turn.turn_id,
            status="active",
            supersedes=candidate.supersedes,
            last_confirmed_at=now,
        )
        return ("updated", message) if success else ("error", message)

    @classmethod
    def _is_sensitive(cls, candidate: MemoryCandidate) -> bool:
        """Reject likely credentials even when the model misclassifies them."""
        return bool(cls._SENSITIVE_RE.search(f"{candidate.description}\n{candidate.content}"))

    @staticmethod
    def _read_body(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        end = text.find("---", 3)
        return text[end + 3 :].strip() if end >= 0 else text.strip()

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(text.split()).casefold()

    def _audit(
        self,
        event_type: str,
        turn: MemoryTurn,
        candidate: MemoryCandidate | None = None,
        detail: str | None = None,
    ) -> None:
        """Emit metadata-only audit records without affecting the query."""
        if self.audit_hook is None:
            return
        payload: dict[str, Any] = {
            "source_session": turn.session_id,
            "source_turn": turn.turn_id,
        }
        if candidate is not None:
            payload.update(
                {
                    "name": candidate.name,
                    "type": candidate.mem_type.value,
                    "confidence": candidate.confidence,
                }
            )
        if detail:
            payload["detail"] = detail[:500]
        with contextlib.suppress(Exception):
            self.audit_hook(event_type, payload)

    def get_stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "min_confidence": self.min_confidence,
            "extraction_timeout": self.extraction_timeout,
            "indexed": len(self.recall.index.memories),
            **self._stats,
        }
