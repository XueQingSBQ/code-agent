"""CLI session discovery output tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from carbide.cli.app import build_parser, list_sessions
from carbide.governance import SessionStore


@pytest.mark.asyncio
async def test_list_sessions_shows_summary_and_pagination(tmp_path: Path) -> None:
    with SessionStore(tmp_path / ".carbide" / "sessions.db") as store:
        store.create_session("session-1", tmp_path)
        store.append_message(
            "session-1", "user", [{"type": "text", "text": "Implement login"}]
        )
        store.create_session("session-2", tmp_path)
        store.append_message(
            "session-2", "user", [{"type": "text", "text": "Improve dashboard"}]
        )

    console = Console(record=True, width=140)
    await list_sessions(tmp_path, None, console, limit=1)

    output = console.export_text()
    assert "session-2" in output
    assert "Improve dashboard" in output
    assert "Next page" in output
    assert "Messages" not in output


def test_list_sessions_parser_supports_paging_and_search() -> None:
    args = build_parser().parse_args(
        ["list-sessions", "--limit", "5", "--offset", "10", "--search", "login"]
    )

    assert args.limit == 5
    assert args.offset == 10
    assert args.search == "login"
