"""P0 Todo invariants and integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from carbide.engine.loop import QueryLoop
from carbide.governance.session_store import SCHEMA_VERSION, SessionStore
from carbide.todo.domain import TodoStatus
from carbide.todo.errors import TodoConflictError, TodoDependencyError, TodoPlanExistsError
from carbide.todo.repository import TodoRepository
from carbide.todo.service import TodoService
from carbide.tools.base import ToolContext
from carbide.tools.registry import ToolRegistry
from carbide.tools.todo import register_todo_tools


def make_service(tmp_path: Path) -> tuple[SessionStore, TodoService]:
    store = SessionStore(tmp_path / "sessions.db")
    store.open()
    store.create_session("session-1", tmp_path)
    service = TodoService(TodoRepository(store), "session-1")
    return store, service


def test_session_schema_is_migrated_and_todo_is_session_scoped(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        assert store.schema_version() == SCHEMA_VERSION
        assert service.snapshot() is None
        initialized = service.initialize()
        assert initialized.plan_version == 0
        with pytest.raises(TodoPlanExistsError):
            service.initialize()
    finally:
        store.close()


def test_todo_survives_session_backup_and_restore(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    backup = tmp_path / "sessions-backup.db"
    restored = tmp_path / "restored.db"
    try:
        service.initialize()
        service.add("Persist this task", expected_version=0)
        store.backup(backup)
    finally:
        store.close()

    SessionStore.restore_backup(backup, restored)
    with SessionStore(restored) as restored_store:
        repository = TodoRepository(restored_store)
        with repository.connection() as conn:
            snapshot = repository.load_snapshot(conn, "session-1")
        assert snapshot is not None
        assert snapshot.items[0].title == "Persist this task"


def test_dependencies_compute_ready_and_blocked_without_storing_ready(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        service.initialize()
        first = service.add("Build API", expected_version=0)
        second = service.add("Write tests", dependencies=[first.items[0].id], expected_version=1)

        item_map = second.item_map
        assert item_map[first.items[0].id].status is TodoStatus.PENDING
        assert item_map[second.items[1].id].effective_status(item_map) == "blocked"

        completed = service.update(
            first.items[0].id,
            status=TodoStatus.COMPLETED,
            expected_version=2,
        )
        assert completed.item_map[second.items[1].id].effective_status(completed.item_map) == "ready"
    finally:
        store.close()


def test_only_one_worker_can_claim_a_ready_task(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    worker_store = SessionStore(tmp_path / "sessions.db")
    worker_store.open()
    try:
        service.initialize()
        service.add("Single worker task", expected_version=0)
        worker_service = TodoService(TodoRepository(worker_store), "session-1", actor_id="worker-b")

        first = service.claim_next("worker-a", lease_seconds=60)
        second = worker_service.claim_next("worker-b", lease_seconds=60)

        assert first is not None
        assert second is None
        assert first.item.owner_id == "worker-a"
        assert first.item.status is TodoStatus.IN_PROGRESS

        completed = service.update(
            first.item.id,
            status=TodoStatus.COMPLETED,
            expected_version=first.plan_version,
            expected_revision=first.item.revision,
            owner_id="worker-a",
        )
        assert completed.item_map[first.item.id].status is TodoStatus.COMPLETED

        with pytest.raises(TodoConflictError, match="owned by another"):
            worker_service.update(
                first.item.id,
                status=TodoStatus.COMPLETED,
                expected_version=completed.plan_version,
                expected_revision=completed.item_map[first.item.id].revision,
                owner_id="worker-b",
            )
    finally:
        worker_store.close()
        store.close()


def test_expired_worker_claim_is_requeued(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        service.initialize()
        service.add("Requeue after crash", expected_version=0)
        claim = service.claim_next("crashed-worker", lease_seconds=60)
        assert claim is not None
        with store.connection() as connection:
            connection.execute(
                "UPDATE todo_items SET lease_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", claim.item.id),
            )
            connection.commit()

        assert service.reap_expired_claims() == (claim.item.id,)
        replacement = service.claim_next("replacement-worker", lease_seconds=60)
        assert replacement is not None
        assert replacement.item.id == claim.item.id
        assert replacement.item.owner_id == "replacement-worker"
    finally:
        store.close()


def test_cycle_and_version_conflicts_are_atomic(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        service.initialize()
        first = service.add("First", expected_version=0)
        second = service.add("Second", expected_version=1)
        first_id = first.items[0].id
        second_id = second.items[1].id
        service.update(first_id, dependencies=[second_id], expected_version=2)

        before = service.snapshot()
        assert before is not None
        with pytest.raises(TodoDependencyError, match="cycle"):
            service.update(second_id, dependencies=[first_id], expected_version=3)
        after_cycle = service.snapshot()
        assert after_cycle is not None
        assert after_cycle.plan_version == before.plan_version
        assert after_cycle.to_dict() == before.to_dict()

        with pytest.raises(TodoConflictError):
            service.add("Stale write", expected_version=2)
        after_conflict = service.snapshot()
        assert after_conflict is not None
        assert after_conflict.plan_version == before.plan_version
    finally:
        store.close()


def test_checkpoint_rollback_restores_only_todo_state(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        service.initialize()
        initial = service.add("Initial task", expected_version=0)
        checkpoint = service.create_checkpoint("before changes", expected_version=1)
        service.update(
            initial.items[0].id,
            status=TodoStatus.COMPLETED,
            expected_version=1,
        )
        service.add("Later task", expected_version=2)

        restored = service.rollback(checkpoint.id, expected_version=3)
        assert restored.plan_version == 4
        assert [item.title for item in restored.items] == ["Initial task"]
        assert restored.items[0].status is TodoStatus.PENDING
        assert len(service.list_checkpoints()) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_model_tools_use_the_same_service_and_context_session(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        registry = ToolRegistry()
        register_todo_tools(registry, service)
        ctx = ToolContext(workspace=tmp_path, session_id="session-1")

        initialized = await registry.invoke("todo_init", {}, ctx)
        assert not initialized.is_error
        added = await registry.invoke(
            "todo_add",
            {"title": "Run tests", "expected_version": 0},
            ctx,
        )
        assert not added.is_error
        listed = await registry.invoke("todo_list", {"max_chars": 8000}, ctx)
        assert "Run tests" in listed.content
        assert "version 1" in listed.content

        mismatch = await registry.invoke(
            "todo_list",
            {"max_chars": 8000},
            ToolContext(workspace=tmp_path, session_id="other"),
        )
        assert mismatch.is_error
        assert "session_id mismatch" in mismatch.content
    finally:
        store.close()


@pytest.mark.asyncio
async def test_snapshot_injection_is_bounded_to_every_fourth_query(tmp_path: Path) -> None:
    store, service = make_service(tmp_path)
    try:
        service.initialize()
        service.add("Keep context", expected_version=0)
        loop = QueryLoop(provider=cast(Any, object()), todo_service=service)

        async def build_for_query() -> str | list[dict[str, object]]:
            loop._todo_mutation_baseline = service.mutation_count
            return await loop._build_system_prompt("base", "continue")

        first = await build_for_query()
        assert "Keep context" in str(first)
        loop._finish_todo_turn()

        second = await build_for_query()
        assert "Keep context" not in str(second)
        loop._finish_todo_turn()
        third = await build_for_query()
        assert "Keep context" not in str(third)
        loop._finish_todo_turn()
        fourth = await build_for_query()
        assert "Keep context" in str(fourth)
    finally:
        store.close()
