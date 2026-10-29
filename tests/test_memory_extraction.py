"""Tests for the durable-memory extraction and persistence path."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from carbide.engine.loop import QueryEvent
from carbide.infra.config import Config
from carbide.memory.base import MemoryType
from carbide.memory.extractor import MemoryCandidate, MemoryExtractor, MemoryTurn
from carbide.memory.manager import MemoryManager, MemoryWriteMode
from carbide.memory.recall import MemoryRecall
from carbide.memory.worker import MemoryWorker
from carbide.memory.writer import MemoryWriter
from carbide.providers.base import StreamChunk, StreamEnd, StreamTextDelta, Usage
from carbide.sdk import Agent


class ScriptedProvider:
    """A provider that returns one scripted text response per stream call."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        yield StreamChunk(type="content_block_start", content="")
        yield StreamChunk(type="content_block_delta", content=response)
        yield StreamChunk(type="message_stop")


class TypedSelectorProvider:
    """Provider double exposing the current typed streaming seam."""

    async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        del kwargs
        yield StreamTextDelta("prefers-pytest")
        yield StreamEnd(usage=Usage())

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def extraction_json(
    *,
    name: str = "prefers-pytest",
    content: str = "项目测试统一使用 pytest。",
    confidence: float = 0.96,
) -> str:
    return json.dumps(
        {
            "memories": [
                {
                    "name": name,
                    "description": "项目测试统一使用 pytest",
                    "content": content,
                    "type": "project",
                    "confidence": confidence,
                    "explicit": True,
                    "tags": ["testing", "python"],
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_extractor_parses_fenced_json_and_clamps_values() -> None:
    provider = ScriptedProvider(
        [
            "```json\n" + extraction_json(confidence=4.0) + "\n```",
        ]
    )
    extractor = MemoryExtractor(provider)

    candidates = await extractor.extract(
        MemoryTurn(
            user_message="请记住: 项目测试统一使用 pytest",
            assistant_message="好的。",
        )
    )

    assert len(candidates) == 1
    assert candidates[0].name == "prefers-pytest"
    assert candidates[0].confidence == 1.0
    assert candidates[0].mem_type is MemoryType.PROJECT
    assert extractor.should_extract("请分析这个函数") is False


@pytest.mark.asyncio
async def test_extractor_reads_provider_neutral_text_deltas() -> None:
    class TypedProvider:
        async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
            del kwargs
            yield StreamTextDelta(extraction_json())
            yield StreamEnd(usage=Usage())

    candidates = await MemoryExtractor(TypedProvider()).extract(
        MemoryTurn("请记住项目测试使用 pytest", "已确认。")
    )

    assert [candidate.name for candidate in candidates] == ["prefers-pytest"]


@pytest.mark.asyncio
async def test_manager_persists_and_refreshes_recall_index(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".carbide" / "memory"
    provider = ScriptedProvider([extraction_json()])
    recall = MemoryRecall(memory_dir)
    audit_events: list[tuple[str, dict[str, Any]]] = []
    manager = MemoryManager(
        memory_dir,
        provider,
        recall=recall,
        audit_hook=lambda event, payload: audit_events.append((event, payload)),
    )

    result = await manager.process_turn(
        MemoryTurn(
            user_message="请记住: 项目测试统一使用 pytest",
            assistant_message="已记录。",
            session_id="session-1",
            turn_id=2,
        )
    )

    path = memory_dir / "prefers-pytest.md"
    assert result.saved == ["prefers-pytest"]
    assert path.exists()
    assert "confidence: 0.96" in path.read_text(encoding="utf-8")
    assert "source_session: session-1" in path.read_text(encoding="utf-8")
    assert "prefers-pytest" in recall.index.memories
    assert manager.get_stats()["saved"] == 1
    assert [event for event, _ in audit_events] == [
        "memory_extraction_started",
        "memory_saved",
    ]
    assert "content" not in audit_events[-1][1]


@pytest.mark.asyncio
async def test_manager_updates_existing_memory_without_duplicates(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    provider = ScriptedProvider(
        [
            extraction_json(content="项目测试统一使用 pytest。"),
            extraction_json(content="项目测试统一使用 pytest, 并要求覆盖率达到 90%。"),
        ]
    )
    manager = MemoryManager(memory_dir, provider)

    first = MemoryTurn("请记住项目测试规范", "已记录。", turn_id=1)
    second = MemoryTurn("以后记住新的项目测试规范", "已更新。", turn_id=2)
    first_result = await manager.process_turn(first)
    second_result = await manager.process_turn(second)

    path = memory_dir / "prefers-pytest.md"
    assert first_result.saved == ["prefers-pytest"]
    assert second_result.updated == ["prefers-pytest"]
    assert "90%" in path.read_text(encoding="utf-8")
    assert len(list(memory_dir.glob("*.md"))) == 1


@pytest.mark.asyncio
async def test_manager_finds_existing_file_created_before_manager(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    writer = MemoryWriter(memory_dir)
    success, _ = writer.create(
        name="prefers-pytest",
        description="旧测试约定",
        content="项目测试统一使用 unittest。",
        mem_type=MemoryType.PROJECT,
    )
    assert success is True

    manager = MemoryManager(memory_dir, ScriptedProvider([extraction_json()]))
    result = await manager.process_turn(MemoryTurn("请记住项目测试规范", "已确认。", turn_id=4))

    assert result.saved == []
    assert result.updated == ["prefers-pytest"]
    assert "pytest" in (memory_dir / "prefers-pytest.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_suggest_candidates_can_be_persisted_after_confirmation(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    manager = MemoryManager(
        memory_dir,
        ScriptedProvider([extraction_json(confidence=0.2)]),
        mode=MemoryWriteMode.SUGGEST,
    )
    turn = MemoryTurn("请记住这个项目约定", "好的。")

    suggestion = await manager.process_turn(turn)
    assert suggestion.suggested == ["prefers-pytest"]
    assert not (memory_dir / "prefers-pytest.md").exists()

    persisted = await manager.persist_candidates(suggestion.candidates, turn)
    assert persisted.saved == ["prefers-pytest"]
    assert (memory_dir / "prefers-pytest.md").exists()


@pytest.mark.asyncio
async def test_suggest_names_can_be_confirmed_without_exposing_candidate_objects(
    tmp_path: Path,
) -> None:
    manager = MemoryManager(
        tmp_path / "memory",
        ScriptedProvider([extraction_json()]),
        mode=MemoryWriteMode.SUGGEST,
    )
    await manager.process_turn(MemoryTurn("请记住这个项目约定", "好的。"))

    result = await manager.persist_suggestions(["prefers-pytest"])

    assert result.saved == ["prefers-pytest"]


def test_writer_rejects_path_traversal_and_frontmatter_injection(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    writer = MemoryWriter(memory_dir)

    success, error = writer.create(
        name="../outside",
        description="safe",
        content="content",
        mem_type=MemoryType.USER,
    )
    assert success is False
    assert "kebab-case" in error.lower()

    success, error = writer.create(
        name="safe-name",
        description="safe\nmalicious: true",
        content="content",
        mem_type=MemoryType.USER,
    )
    assert success is False
    assert "one line" in error.lower()

    success, error = writer.update("../outside", content="changed")
    assert success is False
    assert "kebab-case" in error.lower()
    assert not (tmp_path / "outside.md").exists()

    success, error = writer.delete("../outside")
    assert success is False
    assert "kebab-case" in error.lower()


@pytest.mark.asyncio
async def test_recall_skips_oversized_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    writer = MemoryWriter(memory_dir)
    huge_content = "x" * 25_001
    success, _ = writer.create(
        name="too-large",
        description="large memory",
        content=huge_content,
        mem_type=MemoryType.USER,
    )
    assert success is False

    # A hand-edited oversized file can still be indexed from its header, but
    # must never be loaded into the system prompt.
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "too-large.md").write_text(
        "---\nname: too-large\ndescription: large memory\ntype: user\n---\n" + huge_content,
        encoding="utf-8",
    )
    recall = MemoryRecall(memory_dir)
    recall.scan()
    memories = await recall.recall("large", lambda prompt, max_tokens: _empty_selector())
    assert memories == []


@pytest.mark.asyncio
async def test_recall_selector_timeout_uses_relevant_fallback(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    writer = MemoryWriter(memory_dir)
    for name, description in (
        ("unrelated", "Unrelated project note"),
        ("prefers-pytest", "Project testing uses pytest"),
    ):
        success, _ = writer.create(
            name=name,
            description=description,
            content=description,
            mem_type=MemoryType.PROJECT,
        )
        assert success is True

    async def slow_selector(prompt: str, max_tokens: int) -> str:
        await asyncio.sleep(0.05)
        return "unrelated"

    recall = MemoryRecall(memory_dir, max_recalled=1, selector_timeout=0.001)
    recall.scan()
    memories = await recall.recall("prefers-pytest", slow_selector)

    assert [memory.metadata.name for memory in memories] == ["prefers-pytest"]


@pytest.mark.asyncio
async def test_recall_selector_consumes_typed_provider_deltas(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    writer = MemoryWriter(memory_dir)
    success, _ = writer.create(
        name="prefers-pytest",
        description="Project testing uses pytest",
        content="Project testing uses pytest",
        mem_type=MemoryType.PROJECT,
    )
    assert success is True

    recall = MemoryRecall(memory_dir)
    recall.scan()
    from carbide.engine.loop import QueryLoop

    loop = QueryLoop(provider=TypedSelectorProvider(), workspace=tmp_path)  # type: ignore[arg-type]
    loop.memory_recall = recall

    memories = await loop._recall_memories("project testing")

    assert [memory.metadata.name for memory in memories] == ["prefers-pytest"]


async def _empty_selector() -> str:
    return "too-large"


def test_config_reads_memory_write_mode_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=test-key\nCARBIDE_MEMORY_WRITE_MODE=suggest\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    config = Config.load(workspace=tmp_path)

    assert config.memory_write_mode == "suggest"


@pytest.mark.asyncio
async def test_manager_skips_non_memory_turn_without_provider_call(tmp_path: Path) -> None:
    provider = ScriptedProvider([extraction_json()])
    manager = MemoryManager(tmp_path / "memory", provider)

    result = await manager.process_turn(MemoryTurn("请分析 hello.py", "它打印 hello。"))

    assert result.attempted is False
    assert provider.calls == []
    assert list((tmp_path / "memory").glob("*.md")) == []


@pytest.mark.asyncio
async def test_extraction_failure_does_not_escape_manager(tmp_path: Path) -> None:
    provider = ScriptedProvider([RuntimeError("provider unavailable")])
    manager = MemoryManager(tmp_path / "memory", provider)

    result = await manager.process_turn(MemoryTurn("请记住这个规则", "好的。"))

    assert result.attempted is True
    assert result.saved == []
    assert len(result.errors) == 1
    assert manager.get_stats()["errors"] == 1


@pytest.mark.asyncio
async def test_extraction_timeout_is_bounded_and_reported(tmp_path: Path) -> None:
    class SlowExtractor:
        def should_extract(self, user_message: str) -> bool:
            return True

        async def extract(self, turn: MemoryTurn) -> list[MemoryCandidate]:
            await asyncio.sleep(0.05)
            return []

    manager = MemoryManager(
        tmp_path / "memory",
        ScriptedProvider([]),
        extraction_timeout=0.001,
        extractor=SlowExtractor(),
    )

    result = await manager.process_turn(MemoryTurn("请记住这个规则", "好的。"))

    assert result.errors == ["TimeoutError"]
    assert manager.get_stats()["errors"] == 1


@pytest.mark.asyncio
async def test_manager_rejects_sensitive_memory_even_when_explicit(tmp_path: Path) -> None:
    sensitive = extraction_json(
        name="api-key",
        content="Do not lose api_key=sk-12345678901234567890",
    )
    manager = MemoryManager(tmp_path / "memory", ScriptedProvider([sensitive]))

    result = await manager.process_turn(MemoryTurn("请记住这个 api key", "已记录。"))

    assert result.saved == []
    assert result.updated == []
    assert result.skipped == ["api-key: sensitive-content"]


def test_recall_does_not_offer_superseded_memories(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "old.md").write_text(
        "---\nname: old\ndescription: old rule\ntype: project\nstatus: superseded\n---\nold",
        encoding="utf-8",
    )
    (memory_dir / "active.md").write_text(
        "---\nname: active\ndescription: active rule\ntype: project\nstatus: active\n---\nactive",
        encoding="utf-8",
    )

    recall = MemoryRecall(memory_dir)
    recall.scan()
    candidates = recall._build_candidate_list()

    assert [candidate.name for candidate in candidates] == ["active"]


def test_writer_round_trip_provenance_and_atomic_validation(tmp_path: Path) -> None:
    writer = MemoryWriter(tmp_path / "memory")
    success, _ = writer.create(
        name="user-style",
        description="用户偏好简洁回答",
        content="用户偏好简洁回答。",
        mem_type=MemoryType.USER,
        confidence=0.91,
        source_session="s-1",
        source_turn=3,
        tags=["style"],
    )
    assert success is True

    recall = MemoryRecall(tmp_path / "memory")
    recall.scan()
    metadata = recall.index.get("user-style")
    assert metadata is not None
    assert metadata.confidence == 0.91
    assert metadata.source_session == "s-1"
    assert metadata.source_turn == 3
    assert metadata.tags == ["style"]


@pytest.mark.asyncio
async def test_agent_query_queues_extraction_after_final_answer(tmp_path: Path) -> None:
    provider = ScriptedProvider(["已完成。", extraction_json()])
    agent = Agent(
        provider=provider,
        project_dir=tmp_path,
        enable_memory=True,
        memory_write_mode=MemoryWriteMode.AUTO,
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
        enable_governance=False,
        enable_session_store=False,
    )

    events: list[QueryEvent] = []
    async for event in agent.query("请记住: 项目测试统一使用 pytest"):
        events.append(event)

    queued = [event for event in events if event.type == "memory_queued"]
    assert len(queued) == 1
    assert events[-1].type == "turn_complete"

    updates = await agent.flush_memory()
    assert updates[0]["saved"] == ["prefers-pytest"]
    assert (tmp_path / ".carbide" / "memory" / "prefers-pytest.md").exists()
    assert len(provider.calls) == 2
    await agent.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_memory_worker_retries_and_clears_pending_job(tmp_path: Path) -> None:
    provider = ScriptedProvider([RuntimeError("temporary"), extraction_json()])
    manager = MemoryManager(tmp_path / "memory", provider)
    worker = MemoryWorker(manager, max_retries=1, retry_backoff=0)

    job = await worker.submit(MemoryTurn("请记住项目测试规范", "已确认。", turn_id=1))
    assert job is not None
    await worker.drain(timeout=2)

    updates = worker.pop_results()
    assert updates[0].status == "saved"
    assert worker.get_stats()["retried"] == 1
    assert list((tmp_path / "memory" / ".queue" / "pending").glob("*.json")) == []
    await worker.close()


@pytest.mark.asyncio
async def test_memory_worker_moves_exhausted_job_to_failed_queue(tmp_path: Path) -> None:
    provider = ScriptedProvider([RuntimeError("permanent"), RuntimeError("permanent")])
    manager = MemoryManager(tmp_path / "memory", provider)
    worker = MemoryWorker(manager, max_retries=1, retry_backoff=0)

    await worker.submit(MemoryTurn("请记住项目测试规范", "已确认。", turn_id=1))
    await worker.drain(timeout=2)

    updates = worker.pop_results()
    assert updates[0].status == "failed"
    assert list((tmp_path / "memory" / ".queue" / "failed").glob("*.json"))
    await worker.close()
