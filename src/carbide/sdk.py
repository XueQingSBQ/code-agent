"""SDK: Public API for Carbide agent.

Usage:
    from carbide import Agent

    agent = Agent()
    async for event in agent.query("Fix the bug in main.py"):
        if event.type == "chunk":
            print(event.data.content, end="")
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from carbide.context.manager import CompressionManager
from carbide.engine.loop import QueryEvent, QueryLoop
from carbide.memory.manager import MemoryManager
from carbide.memory.recall import MemoryRecall
from carbide.memory.worker import MemoryWorker
from carbide.process.manager import BackgroundJobManager
from carbide.prompts import DEFAULT_SYSTEM_PROMPT
from carbide.providers.anthropic import create_anthropic_provider
from carbide.providers.base import Provider
from carbide.rules.manager import RuleManager
from carbide.skills.manager import SkillManager
from carbide.skills.paths import agent_skills_dir as default_agent_skills_dir

if TYPE_CHECKING:
    from carbide.agents.manager import SubagentManager


def default_registry() -> Any:
    """Return the standard tool registry.

    Kept at the SDK boundary for integrations that used
    ``carbide.sdk.default_registry`` before the registry factory moved to
    ``carbide.tools.defaults``.
    """

    from carbide.tools.defaults import create_default_registry

    return create_default_registry()


class Agent:
    """
    Main agent interface.

    Encapsulates all subsystems and provides simple query API.
    """

    def __init__(
        self,
        provider: Provider | None = None,
        project_dir: Path | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        system_prompt: str | None = None,
        enable_memory: bool = True,
        memory_write_mode: str | None = None,
        enable_skills: bool = True,
        agent_skills_dir: Path | None = None,
        enable_rules: bool = True,
        enable_compression: bool = True,
        enable_governance: bool = True,
        enable_session_store: bool = True,
        enable_subagents: bool = True,
        enable_todo_tools: bool = True,
        close_provider: bool = True,
        config: Any = None,
        tool_registry: Any = None,
        resume_session_id: str | None = None,
        shell: str = "gitbash",
        approval_callback: Any | None = None,
        max_turns: int | None = None,
        max_output_tokens: int | None = None,
        parent_message_provider: Any | None = None,
        session_store_override: Any | None = None,
        todo_service_override: Any | None = None,
        todo_context_session_id: str | None = None,
        todo_owner_id: str | None = None,
        enable_budget_notices: bool = True,
    ) -> None:
        """
        Initialize agent.

        Args:
            provider: LLM provider (defaults to Anthropic)
            project_dir: Project directory for memory, rules, sessions, and tools
            model: Anthropic-compatible model name when provider is omitted
            api_key: Anthropic-compatible API key when provider is omitted
            base_url: Optional Anthropic-compatible endpoint
            system_prompt: Base system prompt
            enable_memory: Enable memory system
            memory_write_mode: Memory persistence policy (off/explicit/suggest/auto)
            enable_skills: Enable skills system
            agent_skills_dir: User-level directory containing Agent skills. If omitted,
                use ``CARBIDE_SKILLS_DIR`` or ``CARBIDE_HOME/skills``.
            enable_rules: Enable rules system
            enable_compression: Enable context compression
            enable_governance: Enable governance components
            enable_session_store: Enable session persistence
            config: Config object (CLI mode) - overrides other parameters
            tool_registry: Tool registry for tool execution
            resume_session_id: Resume from existing session (optional)
        """

        # If config is provided, extract settings from it
        if config is not None:
            from carbide.providers.anthropic import AnthropicProvider

            project_dir = config.workspace
            enable_compression = config.context.enabled
            shell = config.shell
            memory_write_mode = getattr(config, "memory_write_mode", memory_write_mode)
            max_turns = config.max_turns
            max_output_tokens = config.max_output_tokens

            # Create provider from config
            provider = AnthropicProvider(
                api_key=config.api_key,
                model=config.model,
                base_url=config.base_url,
            )

        # Provider
        self.provider = provider or create_anthropic_provider(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
        self._owns_provider = close_provider

        # Project directory
        self.project_dir = project_dir or Path.cwd()
        # One identity is shared by the session database, audit log, memory
        # events, and the query loop.  Resume keeps the caller-supplied ID;
        # new agents allocate it before any subsystem is initialised.
        self.session_id: str | None = resume_session_id or str(uuid.uuid4())
        if self.session_id is None:  # defensive; UUID generation above is total
            raise RuntimeError("Agent session ID was not initialised")
        self.background_job_manager = BackgroundJobManager(
            self.project_dir,
            self.session_id,
            shell_backend=shell,
        )
        self._resume_messages: list[dict[str, Any]] | None = None
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._last_exit_reason: str | None = None
        self._queries_started = 0
        self.subagent_manager: SubagentManager | None = None
        self._owns_session_store = True
        memory_write_mode = memory_write_mode or "auto"

        # System prompt
        self.system_prompt = system_prompt or self._default_system_prompt()

        # Initialize subsystems
        self.compression_manager = None
        if enable_compression:
            from carbide.context.budget import TokenBudget

            # Create spill directory
            spill_dir = self.project_dir / ".carbide" / "spill"
            spill_dir.mkdir(parents=True, exist_ok=True)

            # Initialize compression with default budget (200K tokens, 80% threshold)
            budget = TokenBudget(max_tokens=200_000, compression_threshold=0.8)
            self.compression_manager = CompressionManager(
                workspace=self.project_dir,
                budget=budget,
                spill_dir=spill_dir,
            )
            if tool_registry is not None and "snip" not in tool_registry:
                from carbide.tools.snip import SnipTool

                tool_registry.register(SnipTool(self.compression_manager.l2_snip))

        self.memory_recall = None
        self.memory_manager = None
        self.memory_worker = None
        if enable_memory:
            memory_dir = self.project_dir / ".carbide" / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            self.memory_recall = MemoryRecall(memory_dir)
            self.memory_recall.scan()
            self.memory_manager = MemoryManager(
                memory_dir=memory_dir,
                provider=self.provider,
                mode=memory_write_mode,
                recall=self.memory_recall,
            )
            self.memory_worker = MemoryWorker(
                self.memory_manager,
                task_tracker=self._track_task,
            )

        self.skill_manager = None
        self.agent_skills_dir = (
            (agent_skills_dir or default_agent_skills_dir()).expanduser().resolve()
        )
        if enable_skills:
            self.skill_manager = SkillManager(self.agent_skills_dir)
            self.skill_manager.scan()

            # The model can discover and load skills autonomously through the
            # normal governed tool path. Do not override a caller's custom
            # implementation with the built-in loader.
            if tool_registry is not None and "load_skill" not in tool_registry:
                from carbide.skills.tool import LoadSkillTool

                tool_registry.register(LoadSkillTool(self.skill_manager))

        self.rule_manager = None
        if enable_rules:
            rules_dir = self.project_dir / ".carbide" / "rules"
            if rules_dir.exists():
                self.rule_manager = RuleManager(rules_dir)
                self.rule_manager.scan()

        # Initialize governance components
        self.permission_engine = None
        self.hook_registry = None
        self.audit_logger = None
        self.bash_guard = None

        if enable_governance:
            from carbide.governance import (
                AuditLogger,
                HookRegistry,
                PermissionEngine,
                PermissionMode,
            )
            from carbide.governance.bashguard import BashGuard

            # Core governance is fail-fast: silently dropping permission or
            # audit enforcement makes the runtime unsafe and impossible to
            # diagnose.
            self.permission_engine = PermissionEngine(mode=PermissionMode.PROMPT)

            audit_dir = self.project_dir / ".carbide" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            if self.session_id is None:  # defensive; UUID generation above is total
                raise RuntimeError("Agent session ID was not initialised")
            audit_log_path = audit_dir / f"{self.session_id}.jsonl"
            self.audit_logger = AuditLogger(
                log_path=audit_log_path,
                session_id=self.session_id,
                max_bytes=getattr(config, "audit_max_bytes", 10_000_000),
                backup_count=getattr(config, "audit_backup_count", 5),
                max_field_chars=getattr(config, "audit_max_field_chars", 16_384),
            )
            self.audit_logger.open()

            # BashGuard has no external state and must remain available even
            # when an optional project hook directory is malformed.
            self.bash_guard = BashGuard()

            hooks_dir = self.project_dir / ".carbide" / "hooks"
            self.hook_registry = HookRegistry(hooks_dir if hooks_dir.exists() else None)
            try:
                self.hook_registry.scan()
            except (OSError, ValueError) as exc:
                # A project hook is optional.  Disable only hooks and make the
                # degradation visible; permission, BashGuard and audit remain
                # active.
                import warnings

                self.hook_registry = None
                warnings.warn(
                    f"Project hooks disabled during startup: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

        audit_logger = self.audit_logger
        if self.memory_manager is not None and audit_logger is not None:

            def audit_memory_event(event_type: str, payload: dict[str, Any]) -> None:
                """Bridge metadata-only Memory events into governance audit logs."""
                import json
                from datetime import UTC, datetime

                from carbide.governance.audit import AuditEntry

                audit_logger.log(
                    AuditEntry(
                        timestamp=datetime.now(UTC).isoformat(),
                        session_id=self.session_id or "unknown",
                        event_type=event_type,
                        tool_name="memory",
                        result=json.dumps(payload, ensure_ascii=False),
                        is_error=event_type.endswith("failed"),
                    )
                )

            self.memory_manager.audit_hook = audit_memory_event

        # Initialize session store
        self.session_store = None
        self.todo_service = None
        if enable_session_store:
            from carbide.governance.session_store import SessionStore
            from carbide.todo.repository import TodoRepository
            from carbide.todo.service import TodoService

            store_path = self.project_dir / ".carbide" / "sessions.db"
            # The Agent performs short SQLite operations from an async event
            # loop. Release the file handle after each operation so an
            # un-awaited/legacy caller cannot lock a Windows workspace.
            if session_store_override is not None:
                self.session_store = session_store_override
                self._owns_session_store = False
            else:
                self.session_store = SessionStore(store_path, ephemeral=True)
                self.session_store.open()

            if self.session_id is None:
                raise RuntimeError("Agent session ID was not initialised")
            if resume_session_id:
                if not self.session_store.session_exists(resume_session_id):
                    raise ValueError(f"Session {resume_session_id} not found")
                self._resume_messages = self.session_store.load_messages(resume_session_id)
            else:
                self.session_store.create_session(
                    session_id=self.session_id,
                    workspace=self.project_dir,
                )

            self.todo_service = todo_service_override or TodoService(
                TodoRepository(self.session_store), self.session_id, actor_id="agent"
            )
            if tool_registry is not None and enable_todo_tools:
                from carbide.tools.todo import register_todo_tools

                register_todo_tools(tool_registry, self.todo_service)
        elif resume_session_id:
            raise ValueError("resume_session_id requires enable_session_store=True")

        # Query loop with governance
        self.loop = QueryLoop(
            provider=self.provider,
            compression_manager=self.compression_manager,
            memory_recall=self.memory_recall,
            memory_manager=self.memory_manager,
            memory_worker=self.memory_worker,
            skill_manager=self.skill_manager,
            rule_manager=self.rule_manager,
            tool_registry=tool_registry,
            workspace=self.project_dir,
            permission_engine=self.permission_engine,
            hook_registry=self.hook_registry,
            audit_logger=self.audit_logger,
            bash_guard=self.bash_guard,
            session_store=self.session_store,
            session_id=self.session_id,
            todo_service=self.todo_service,
            todo_session_id=todo_context_session_id or self.session_id,
            todo_owner_id=todo_owner_id,
            shell=shell,
            approval_callback=approval_callback,
            task_tracker=self._track_task,
            background_job_manager=self.background_job_manager,
            # ``None`` means no agent-turn limit.  Keep an explicit value as
            # an opt-in safety valve for callers that need bounded runs.
            max_turns=max_turns,
            max_output_tokens=max_output_tokens or 8_000,
            parent_message_provider=parent_message_provider,
            enable_budget_notices=enable_budget_notices,
        )

        # Restore messages if resuming
        if self._resume_messages:
            from carbide.engine.repair import repair_messages

            repaired_messages, repaired_count = repair_messages(self._resume_messages)
            self.loop.state.messages.extend(repaired_messages)
            if repaired_count and self.session_store and self.session_id:
                self.session_store.replace_messages(self.session_id, repaired_messages)

        if enable_subagents and tool_registry is not None:
            from carbide.agents.manager import SubagentManager
            from carbide.agents.tools import register_subagent_tools

            self.subagent_manager = SubagentManager(self)
            register_subagent_tools(tool_registry, self.subagent_manager)
            # Child-to-parent mailbox messages are injected only at model
            # request boundaries, preserving stream/tool coherence.
            self.loop.child_message_provider = self.subagent_manager.parent_messages

    def poll_subagent_updates(self) -> list[dict[str, Any]]:
        """Return completed background child results without waiting."""
        if self.subagent_manager is None:
            return []
        return self.subagent_manager.poll_completed()

    def get_subagent_status(self, agent_id: str) -> dict[str, Any] | None:
        """Return one durable child run record."""
        if self.subagent_manager is None:
            return None
        return self.subagent_manager.status(agent_id)

    def send_subagent_message(self, agent_id: str, *, kind: str, content: Any) -> str:
        """Send a durable plan update or answer to a child Agent."""
        if self.subagent_manager is None:
            raise RuntimeError("SubAgents are disabled")
        return self.subagent_manager.send(agent_id, kind=kind, content=content)

    def _default_system_prompt(self) -> str:
        """Default system prompt."""
        return DEFAULT_SYSTEM_PROMPT

    async def query(
        self,
        message: str,
        tools: list[dict[str, Any]] | None = None,
        *,
        deadline: float | None = None,
    ) -> AsyncIterator[QueryEvent]:
        """
        Send a query to the agent.

        Args:
            message: User message
            tools: Available tools (optional)
            deadline: Optional planning deadline using asyncio's loop.time().
                Enforce hard timeouts in the host with asyncio.timeout().

        Yields:
            QueryEvent: Events during processing
        """

        self._queries_started += 1
        self._last_exit_reason = None
        if tools is None and self.loop.tool_registry is not None:
            tools = self.loop.tool_registry.api_schemas()
        if self.memory_worker is not None:
            await self.memory_worker.start()
        async for event in self.loop.query(
            user_message=message,
            system_prompt=self.system_prompt,
            tools=tools,
            deadline=deadline,
        ):
            if event.type == "error" and isinstance(event.data, dict):
                self._last_exit_reason = str(event.data.get("reason", "provider_error"))
            elif event.type == "turn_complete" and isinstance(event.data, dict):
                reason = event.data.get("reason")
                if reason is not None:
                    self._last_exit_reason = str(reason)
            yield event

    async def flush_memory(self, timeout: float = 35.0) -> list[dict[str, Any]]:  # noqa: ASYNC109
        """Drain queued memory work and return results for a host/UI."""
        if self.memory_worker is None:
            return []
        try:
            await self.memory_worker.drain(timeout=timeout)
        except TimeoutError:
            return [
                {
                    "status": "failed",
                    "errors": ["memory worker drain timed out"],
                }
            ]
        return [result.to_dict() for result in self.memory_worker.pop_results()]

    def poll_memory_updates(self) -> list[dict[str, Any]]:
        """Return completed background memory results without waiting."""
        if self.memory_worker is None:
            return []
        return [result.to_dict() for result in self.memory_worker.pop_results()]

    def reset(self) -> None:
        """Reset conversation state."""
        self.loop.reset()

    def get_stats(self) -> dict[str, Any]:
        """Get agent statistics."""

        state = self.loop.get_state()

        stats: dict[str, Any] = {
            "turn_count": state.turn_count,
            "total_input_tokens": state.total_input_tokens,
            "total_output_tokens": state.total_output_tokens,
            "total_tokens": state.total_input_tokens + state.total_output_tokens,
            "total_cache_creation_tokens": state.total_cache_creation_tokens,
            "total_cache_read_tokens": state.total_cache_read_tokens,
            "message_count": len(state.messages),
        }
        total_prompt_tokens = (
            state.total_input_tokens
            + state.total_cache_creation_tokens
            + state.total_cache_read_tokens
        )
        stats["total_prompt_tokens"] = total_prompt_tokens
        stats["cache_hit_rate"] = (
            state.total_cache_read_tokens / total_prompt_tokens if total_prompt_tokens else 0.0
        )

        # Add subsystem stats
        if self.memory_recall:
            stats["memory"] = {
                "total": len(self.memory_recall.index.memories),
            }
            if self.memory_manager:
                stats["memory"].update(self.memory_manager.get_stats())
            if self.memory_worker:
                stats["memory"]["worker"] = self.memory_worker.get_stats()

        if self.skill_manager:
            stats["skills"] = self.skill_manager.get_stats()

        if self.rule_manager:
            stats["rules"] = self.rule_manager.get_stats()

        if self.subagent_manager is not None:
            stats["subagents"] = self.subagent_manager.stats()

        if self.compression_manager:
            stats["compression_enabled"] = True
        else:
            stats["compression_enabled"] = False

        if hasattr(self.loop, "metrics_collector"):
            stats["execution_metrics"] = self.loop.metrics_collector.get_summary()

        return stats

    async def __aenter__(self) -> Agent:
        """Async context manager entry."""
        if self.memory_worker is not None:
            await self.memory_worker.start()
        return self

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        """Track tasks created by this agent and forget them when complete."""
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        # Update session status
        if self.session_store and self.session_id:
            try:
                if exc_type is None and (
                    self._queries_started == 0 or self._last_exit_reason == "completed"
                ):
                    # An untouched agent and a naturally completed query are
                    # both successful.  QueryLoop failures are represented as
                    # events rather than Python exceptions, so consult the
                    # recorded terminal reason before marking the session.
                    self.session_store.update_session_status(self.session_id, "completed")
                else:
                    # Provider/tool/compaction/limit exits remain resumable,
                    # but must not be advertised as successful work.
                    self.session_store.update_session_status(self.session_id, "failed")
            except Exception:
                pass

        # Reclaim attached child processes before closing the transport shell.
        # Explicitly detached jobs remain available for a later `jobs` command.
        with contextlib.suppress(Exception):
            await self.background_job_manager.close()

        if self.subagent_manager is not None:
            with contextlib.suppress(Exception):
                await self.subagent_manager.close()

        # Close all BashSessions
        with contextlib.suppress(Exception):
            from carbide.tools.shell import BashTool

            await BashTool.close_sessions()

        # Memory jobs are journaled and must be given a bounded graceful
        # shutdown window before owned tasks are cancelled.
        if self.memory_worker is not None:
            with contextlib.suppress(Exception):
                await self.memory_worker.close()

        # Cancel only tasks explicitly created by this agent.  Cancelling all
        # loop tasks can kill the host application's workers or another Agent.
        tasks = [task for task in self._owned_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()

        # Close audit logger
        if self.audit_logger is not None:
            with contextlib.suppress(Exception):
                self.audit_logger.close()

        # Close session store
        if self.session_store is not None and self._owns_session_store:
            with contextlib.suppress(Exception):
                self.session_store.close()

        # Close provider if it has cleanup
        if self._owns_provider and hasattr(self.provider, "close"):
            with contextlib.suppress(Exception):
                await self.provider.close()


def create_agent(
    project_dir: Path | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> Agent:
    """
    Create an agent with sensible defaults.

    Args:
        project_dir: Project directory
        model: Model name (e.g., "claude-sonnet-4-20250514")
        **kwargs: Additional arguments for Agent()

    Returns:
        Configured Agent instance
    """

    provider = None
    if model:
        provider = create_anthropic_provider(model=model)

    return Agent(
        provider=provider,
        project_dir=project_dir,
        **kwargs,
    )
