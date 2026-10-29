"""Interactive ``/todo`` command adapter.

The command layer deliberately reuses ``TodoService`` so CLI and model tools
cannot drift into two different state machines.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from rich.console import Console

from carbide.todo.domain import TodoStatus
from carbide.todo.errors import TodoError
from carbide.todo.service import TodoService

_USAGE = """Todo commands:
  /todo init
  /todo list
  /todo add \"title\" [--depends id,id]
  /todo update <id> [--status STATUS] [--title \"title\"] [--description \"text\"] [--depends id,id]
  /todo done <id>
  /todo remove <id>
  /todo checkpoint \"label\"
  /todo checkpoints
  /todo rollback <checkpoint-id>
"""


def handle_todo_command(command: str, service: TodoService, console: Console) -> bool:
    """Handle a slash Todo command and return whether it was recognized."""
    if not command.lstrip().startswith("/todo"):
        return False
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        console.print(f"[red]Todo command parse error: {exc}[/red]")
        return True
    if not parts or parts[0] != "/todo":
        return False
    action = parts[1].lower() if len(parts) > 1 else "help"

    try:
        if action == "help":
            console.print(_USAGE.rstrip())
        elif action == "init":
            console.print(service.initialize().render())
        elif action == "list":
            snapshot = service.snapshot()
            console.print(snapshot.render() if snapshot else "No Todo plan initialized.")
        elif action == "add":
            _add(parts[2:], service, console)
        elif action in {"update", "done"}:
            _update(action, parts[2:], service, console)
        elif action == "remove":
            _remove(parts[2:], service, console)
        elif action == "checkpoint":
            _checkpoint(parts[2:], service, console)
        elif action == "checkpoints":
            _checkpoints(service, console)
        elif action == "rollback":
            _rollback(parts[2:], service, console)
        else:
            console.print(f"[yellow]Unknown Todo command: {action}[/yellow]")
            console.print(_USAGE.rstrip())
    except TodoError as exc:
        console.print(f"[red][{exc.code}] {exc}[/red]")
    except ValueError as exc:
        console.print(f"[red]Todo command error: {exc}[/red]")
    return True


def _version(service: TodoService) -> int:
    snapshot = service.snapshot()
    if snapshot is None:
        raise ValueError("Todo plan is not initialized; run /todo init first")
    return snapshot.plan_version


def _add(args: Sequence[str], service: TodoService, console: Console) -> None:
    if not args or args[0].startswith("--"):
        raise ValueError('usage: /todo add "title" [--depends id,id]')
    title = args[0]
    flags = _flags(args[1:])
    dependencies = _split_dependencies(flags.get("depends"))
    snapshot = service.add(
        title,
        dependencies=dependencies,
        expected_version=_version(service),
    )
    console.print(snapshot.render())


def _update(
    action: str,
    args: Sequence[str],
    service: TodoService,
    console: Console,
) -> None:
    if not args:
        raise ValueError("usage: /todo update <id> [flags]")
    item_id = args[0]
    if action == "done":
        if len(args) != 1:
            raise ValueError("usage: /todo done <id>")
        flags: dict[str, str] = {"status": TodoStatus.COMPLETED.value}
    else:
        flags = _flags(args[1:])
    if not flags:
        raise ValueError("update requires at least one field")
    dependencies = _split_dependencies(flags["depends"]) if "depends" in flags else None
    status = TodoStatus(flags["status"]) if "status" in flags else None
    snapshot = service.update(
        item_id,
        title=flags.get("title"),
        description=flags.get("description"),
        status=status,
        dependencies=dependencies,
        expected_version=_version(service),
    )
    console.print(snapshot.render())


def _remove(args: Sequence[str], service: TodoService, console: Console) -> None:
    if len(args) != 1:
        raise ValueError("usage: /todo remove <id>")
    console.print(service.remove(args[0], expected_version=_version(service)).render())


def _checkpoint(args: Sequence[str], service: TodoService, console: Console) -> None:
    if len(args) != 1:
        raise ValueError('usage: /todo checkpoint "label"')
    checkpoint = service.create_checkpoint(args[0], expected_version=_version(service))
    console.print(
        f"Checkpoint created: {checkpoint.id} (plan version {checkpoint.source_plan_version})"
    )


def _checkpoints(service: TodoService, console: Console) -> None:
    checkpoints = service.list_checkpoints()
    if not checkpoints:
        console.print("No Todo checkpoints.")
        return
    for checkpoint in checkpoints:
        console.print(
            f"[{checkpoint.id}] {checkpoint.label} (plan version {checkpoint.source_plan_version})"
        )


def _rollback(args: Sequence[str], service: TodoService, console: Console) -> None:
    if len(args) != 1:
        raise ValueError("usage: /todo rollback <checkpoint-id>")
    snapshot = service.rollback(args[0], expected_version=_version(service))
    console.print(snapshot.render())


def _flags(args: Sequence[str]) -> dict[str, str]:
    flags: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--") or index + 1 >= len(args):
            raise ValueError(f"expected --flag value, got {token!r}")
        key = token[2:].replace("-", "_")
        if key not in {"depends", "status", "title", "description"}:
            raise ValueError(f"unknown Todo option: --{key}")
        flags[key] = args[index + 1]
        index += 2
    return flags


def _split_dependencies(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())
