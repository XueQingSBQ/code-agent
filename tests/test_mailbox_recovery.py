"""Leased transport and atomic, deduplicated durable Agent context delivery."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from carbide.engine.loop import QueryLoop
from carbide.governance.session_store import SCHEMA_VERSION, SessionStore
from carbide.providers.base import StreamEnd, StreamTextDelta
from carbide.sdk import Agent
from tests.conftest import ScriptedProvider


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SessionStore]:
    with SessionStore(tmp_path / "mailbox.db") as opened:
        opened.create_session("parent", tmp_path)
        opened.create_session("child", tmp_path)
        opened.create_session("other", tmp_path)
        opened.create_agent_run(
            run_id="run",
            parent_session_id="parent",
            child_session_id="child",
            mode="worker",
            role=None,
            task="work",
            background=True,
            owner_id="current",
        )
        yield opened


def send(store: SessionStore, *, direction: str = "child_to_parent") -> str:
    return store.append_agent_message(
        run_id="run",
        direction=direction,
        kind="progress",
        content="keep this instruction",
    )


def claim(store: SessionStore, *, lease_seconds: float = 30) -> dict[str, Any]:
    messages = store.claim_agent_messages_for_parent(
        parent_session_id="parent",
        lease_seconds=lease_seconds,
    )
    assert len(messages) == 1
    return messages[0]


def accept(store: SessionStore, message: dict[str, Any], *, session_id: str = "parent") -> bool:
    return store.accept_agent_message(
        session_id=session_id,
        message_id=message["id"],
        delivery_token=message["delivery_token"],
        content=[{"type": "text", "text": message["content"]}],
    )


def status(store: SessionStore, message_id: str) -> str:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT status FROM agent_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.parametrize("failure", ["stale_owner", "duplicate_id", "missing_run"])
def test_failed_message_insert_rolls_back_and_releases_writer(
    store: SessionStore,
    tmp_path: Path,
    failure: str,
) -> None:
    message_id = send(store)
    arguments: dict[str, Any] = {
        "run_id": "run",
        "direction": "child_to_parent",
        "kind": "result",
        "content": "rejected",
    }
    if failure == "stale_owner":
        arguments["owner_id"] = "old"
    elif failure == "duplicate_id":
        arguments["message_id"] = message_id
    else:
        arguments["run_id"] = "missing"
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        store.append_agent_message(**arguments)
    with store.connection() as conn:
        assert not conn.in_transaction
    with store.transaction() as conn:
        conn.execute("UPDATE sessions SET status = 'completed' WHERE id = 'parent'")
    # A second real connection can also acquire the writer lock afterwards.
    with SessionStore(tmp_path / "mailbox.db") as second, second.transaction() as conn:
        conn.execute("UPDATE sessions SET status = 'running' WHERE id = 'parent'")


def test_transaction_rolls_back_cancellation(store: SessionStore) -> None:
    with pytest.raises(asyncio.CancelledError), store.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE id = 'other'")
        raise asyncio.CancelledError
    assert store.session_exists("other")
    with store.transaction():
        pass


@pytest.mark.parametrize("ephemeral", [False, True])
def test_close_releases_unacked_deliveries_but_connection_recycling_does_not(
    store: SessionStore,
    tmp_path: Path,
    ephemeral: bool,
) -> None:
    message_id = send(store)
    consumer = SessionStore(tmp_path / "mailbox.db", ephemeral=ephemeral)
    consumer.open()
    try:
        first = claim(consumer)
        assert consumer.claim_agent_messages_for_parent(parent_session_id="parent") == []
        assert store.claim_agent_messages_for_parent(parent_session_id="parent") == []
    finally:
        consumer.close()
    second = claim(store)
    assert second["id"] == message_id
    assert second["delivery_token"] != first["delivery_token"]
    assert accept(store, second)
    assert not accept(store, second)
    assert len(store.load_messages("parent")) == 1
    assert status(store, message_id) == "acknowledged"


async def test_expired_lease_reclaims_and_fences_old_delivery(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    send(store)
    with SessionStore(tmp_path / "mailbox.db") as second:
        first = claim(store, lease_seconds=0.5)
        assert second.claim_agent_messages_for_parent(parent_session_id="parent") == []
        await asyncio.sleep(0.6)
        with pytest.raises(ValueError, match="delivery lease"):
            accept(store, first)
        renewed = claim(second)
        assert not store.acknowledge_agent_message(
            first["id"],
            delivery_token=first["delivery_token"],
        )
        with pytest.raises(ValueError, match="delivery lease"):
            accept(store, first)
        # Old consumer shutdown must not release a newer consumer's lease.
        store.close()
        assert second.claim_agent_messages_for_parent(parent_session_id="parent") == []
        assert accept(second, renewed)


@pytest.mark.parametrize(
    "direction,recipient",
    [
        ("child_to_parent", "parent"),
        ("parent_to_child", "child"),
    ],
)
def test_atomic_accept_is_scoped_and_deduplicated(
    store: SessionStore,
    direction: str,
    recipient: str,
) -> None:
    send(store, direction=direction)
    message = store.claim_agent_messages(run_id="run", direction=direction)[0]
    with pytest.raises(ValueError, match="recipient"):
        accept(store, message, session_id="other")
    assert store.load_messages(recipient) == []
    assert accept(store, message, session_id=recipient)
    assert not accept(store, message, session_id=recipient)
    with pytest.raises(ValueError, match="recipient"):
        accept(store, message, session_id="other")
    assert len(store.load_messages(recipient)) == 1
    assert store.claim_agent_messages(run_id="run", direction=direction) == []


@pytest.mark.parametrize("failure_point", ["context_insert", "ack_update"])
def test_accept_failure_rolls_back_both_context_and_ack(
    store: SessionStore,
    failure_point: str,
) -> None:
    message_id = send(store)
    message = claim(store)
    table_event = (
        "INSERT ON messages" if failure_point == "context_insert" else "UPDATE ON agent_messages"
    )
    with store.transaction() as conn:
        conn.execute(
            f"""CREATE TRIGGER fail_accept BEFORE {table_event}
                BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""",
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected storage failure"):
        accept(store, message)
    assert store.load_messages("parent") == []
    assert status(store, message_id) == "delivered"
    with store.transaction() as conn:
        conn.execute("DROP TRIGGER fail_accept")
    assert accept(store, message)
    assert len(store.load_messages("parent")) == 1


def test_v6_migration_recovers_only_unacked_messages(store: SessionStore, tmp_path: Path) -> None:
    recoverable = send(store)
    acknowledged = send(store)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE agent_messages SET status = 'delivered' WHERE message_id = ?", (recoverable,)
        )
        conn.execute(
            "UPDATE agent_messages SET status = 'acknowledged' WHERE message_id = ?",
            (acknowledged,),
        )
        for name in ("delivery_owner", "delivery_token", "delivery_until"):
            conn.execute(f"ALTER TABLE agent_messages DROP COLUMN {name}")
        conn.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
    store.close()
    with SessionStore(tmp_path / "mailbox.db") as migrated:
        assert migrated.schema_version() == SCHEMA_VERSION
        message = claim(migrated)
        assert message["id"] == recoverable
        assert accept(migrated, message)
        assert status(migrated, acknowledged) == "acknowledged"


def test_concurrent_consumers_claim_once(store: SessionStore, tmp_path: Path) -> None:
    identifiers = {send(store) for _ in range(24)}
    # Each thread owns a separate SQLite connection, as real consumers do.
    barrier = Barrier(4)

    def consume(_: int) -> list[str]:
        with SessionStore(tmp_path / "mailbox.db") as consumer:
            barrier.wait(timeout=10)
            incoming = consumer.claim_agent_messages_for_parent(parent_session_id="parent")
            for message in incoming:
                assert accept(consumer, message)
            return [str(message["id"]) for message in incoming]

    with ThreadPoolExecutor(max_workers=4) as pool:
        received = [identifier for batch in pool.map(consume, range(4)) for identifier in batch]
    assert len(received) == len(set(received)) == 24
    assert set(received) == identifiers
    assert len(store.load_messages("parent")) == 24


@pytest.mark.parametrize(
    "direction,recipient,source",
    [
        ("child_to_parent", "parent", "child"),
        ("parent_to_child", "child", "parent"),
    ],
)
async def test_queryloop_accepts_once_and_resume_loads_context(
    store: SessionStore,
    tmp_path: Path,
    direction: str,
    recipient: str,
    source: str,
) -> None:
    send(store, direction=direction)
    incoming = store.claim_agent_messages(run_id="run", direction=direction)
    provider = ScriptedProvider([])
    loop = QueryLoop(
        provider=provider, workspace=tmp_path, session_store=store, session_id=recipient
    )
    if source == "parent":

        async def parent_messages() -> list[dict[str, Any]]:
            return incoming

        loop.parent_message_provider = parent_messages
        await loop._inject_parent_messages()
        await loop._inject_parent_messages()
    else:
        loop.child_message_provider = lambda: incoming
        assert len(await loop._inject_child_messages()) == 1
        assert await loop._inject_child_messages() == []
    assert len(loop.state.messages) == 1
    assert loop.state.messages == store.load_messages(recipient)
    with SessionStore(tmp_path / "mailbox.db") as resumed:
        assert resumed.load_messages(recipient) == loop.state.messages
        assert resumed.claim_agent_messages(run_id="run", direction=direction) == []


async def test_queryloop_stops_visibly_on_mailbox_persistence_failure(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    message_id = send(store)
    provider = ScriptedProvider([])
    loop = QueryLoop(
        provider=provider, workspace=tmp_path, session_store=store, session_id="parent"
    )
    loop.child_message_provider = lambda: store.claim_agent_messages_for_parent(
        parent_session_id="parent"
    )
    with store.transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_mailbox BEFORE UPDATE ON agent_messages
                     WHEN NEW.status = 'acknowledged'
                     BEGIN SELECT RAISE(ABORT, 'cannot commit inbox'); END""")
    events = [event async for event in loop.query("continue")]
    assert provider.call_count == 0
    assert [event.data["reason"] for event in events if event.type == "error"] == ["mailbox_error"]
    assert events[-1].data["reason"] == "mailbox_error"
    assert len(loop.state.messages) == 1  # Only the caller's request, not the failed injection.
    assert len(store.load_messages("parent")) == 1
    assert status(store, message_id) == "delivered"


class ExitOnAppend(list[dict[str, Any]]):
    def append(self, value: dict[str, Any]) -> None:
        os._exit(23)  # Simulate death after durable accept, before memory insertion.


def crash_consumer(path: str, phase: str, direction: str, connection: Connection) -> None:
    consumer = SessionStore(Path(path))
    consumer.open()
    incoming = consumer.claim_agent_messages(run_id="run", direction=direction, lease_seconds=0.15)
    connection.send(incoming[0]["id"])
    connection.close()
    if phase == "after_claim":
        os._exit(23)
    recipient = "parent" if direction == "child_to_parent" else "child"
    source = "child" if direction == "child_to_parent" else "parent"
    loop = QueryLoop(
        provider=ScriptedProvider([]),
        workspace=Path(path).parent,
        session_store=consumer,
        session_id=recipient,
    )
    loop.state.messages = ExitOnAppend()
    loop._accept_agent_context(incoming[0], source=source)
    raise AssertionError("expected an abrupt exit")


@pytest.mark.parametrize(
    "direction,recipient",
    [
        ("child_to_parent", "parent"),
        ("parent_to_child", "child"),
    ],
)
@pytest.mark.parametrize("phase", ["after_claim", "after_commit"])
async def test_actual_process_death_does_not_lose_or_duplicate_context(
    store: SessionStore,
    tmp_path: Path,
    direction: str,
    recipient: str,
    phase: str,
) -> None:
    identifier = send(store, direction=direction)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=crash_consumer,
        args=(str(tmp_path / "mailbox.db"), phase, direction, sender),
    )
    try:
        process.start()
        sender.close()
        assert await asyncio.to_thread(receiver.poll, 20)
        assert receiver.recv() == identifier
        await asyncio.to_thread(process.join, 20)
        assert process.exitcode == 23
        await asyncio.sleep(0.2)
        with SessionStore(tmp_path / "mailbox.db") as resumed:
            incoming = resumed.claim_agent_messages(run_id="run", direction=direction)
            if phase == "after_claim":
                assert len(incoming) == 1
                assert accept(resumed, incoming[0], session_id=recipient)
            else:
                assert incoming == []
            assert len(resumed.load_messages(recipient)) == 1
            assert status(resumed, identifier) == "acknowledged"
            provider = ScriptedProvider(
                [
                    [
                        StreamTextDelta("resumed"),
                        StreamEnd(
                            assistant_content=[{"type": "text", "text": "resumed"}],
                        ),
                    ],
                ]
            )
            async with Agent(
                provider=provider,
                project_dir=tmp_path,
                resume_session_id=recipient,
                session_store_override=resumed,
                enable_memory=False,
                enable_skills=False,
                enable_rules=False,
                enable_compression=False,
                enable_governance=False,
                enable_subagents=False,
            ) as agent:
                assert len(agent.loop.state.messages) == 1
                events = [event async for event in agent.query("continue")]
                assert events[-1].data["reason"] == "completed"
                history = str(provider.calls[0]["messages"])
                assert history.count("keep this instruction") == 1
    finally:
        receiver.close()
        sender.close()
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 10)
        process.close()
