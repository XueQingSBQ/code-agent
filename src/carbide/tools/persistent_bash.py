"""Persistent bash tool using BashSession.

Unlike BashTool which spawns a new process per command, this tool maintains
a single long-lived bash session that preserves state (cwd, env vars, shell
functions) across commands.

M1 implementation: adds optional persistent session support. The tool can be
used either in stateless mode (like BashTool) or with a persistent session
that's managed by the executor/agent lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from carbide.governance.permission import RiskLevel
from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.session import BashSession

OUTPUT_LIMIT = 30_000
DEFAULT_TIMEOUT = 120.0


class PersistentBashParams(BaseModel):
    command: str = Field(description="Shell command to run in the persistent session.")
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=600, description="Timeout in seconds.")


class PersistentBashTool(Tool):
    """Bash tool with persistent session support.

    This tool maintains state across invocations when used with a session.
    Directory changes, environment variables, and shell functions persist.
    """

    name: ClassVar[str] = "persistent_bash"
    description: ClassVar[str] = (
        "Run a shell command in a persistent bash session. The session maintains "
        "state across commands: directory changes, environment variables, and shell "
        "functions persist. Use for multi-step workflows that require stateful interaction."
    )
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = True
    # Not concurrency-safe: commands in the same session must execute sequentially
    concurrency_safe: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.DESTRUCTIVE
    params: ClassVar[type[BaseModel]] = PersistentBashParams

    def __init__(self) -> None:
        self._session: BashSession | None = None

    async def get_session(self, workspace: Path) -> BashSession:
        """Get or create the persistent session for this workspace."""
        if self._session is None:
            self._session = BashSession(workspace, timeout=DEFAULT_TIMEOUT)
            await self._session.start()
        return self._session

    async def close_session(self) -> None:
        """Close the persistent session if one exists."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, PersistentBashParams)

        try:
            # Get or create session with workspace-specific timeout
            if self._session is None:
                self._session = BashSession(ctx.workspace, timeout=args.timeout)
                await self._session.start()

            session = self._session
            output, exit_code = await session.execute(args.command)

            # Truncate if output is too large
            if len(output) > OUTPUT_LIMIT:
                head = output[: OUTPUT_LIMIT // 2]
                tail = output[-OUTPUT_LIMIT // 2 :]
                output = (
                    f"{head}\n\n[... {len(output) - OUTPUT_LIMIT} characters elided ...]\n\n{tail}"
                )

            body = output.strip() or "[no output]"
            if exit_code != 0:
                return ToolResult(content=f"exit {exit_code}\n{body}", is_error=True)
            return ToolResult(content=body)

        except TimeoutError:
            return ToolResult(
                content=f"Command timed out after {args.timeout}s.",
                is_error=True,
            )
        except RuntimeError as exc:
            return ToolResult(
                content=f"Session error: {exc}",
                is_error=True,
            )
