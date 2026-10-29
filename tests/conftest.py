from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import pytest

from carbide.context.manager import CompressionManager
from carbide.context.budget import TokenBudget
from carbide.engine.loop import StreamingQueryLoop
from carbide.providers.base import Provider, ProviderError
from carbide.tools.base import ToolContext


_LIVE_MODULES = {
    "integration_baseline/test_baseline.py",
    "integration_baseline/test_baseline_real_api.py",
    "test_1_memory.py",
    "test_2_governance.py",
    "test_3_compression.py",
    "test_api.py",
    "test_api_stability.py",
    "test_cache_cli.py",
    "test_cache_fix.py",
    "test_cli_direct.py",
    "test_cli_error.py",
    "test_cli_manual.py",
    "test_cli_tokens.py",
    "test_complete_integration.py",
    "test_complex_query.py",
    "test_compression_fix.py",
    "test_continuation.py",
    "test_debug_tools.py",
    "test_detailed_verification.py",
    "test_e2e_bash_persistence.py",
    "test_e2e_scenario.py",
    "test_final_verification.py",
    "test_full_integration.py",
    "test_local_api.py",
    "test_m0_m5_integration.py",
    "test_model_output.py",
    "test_real_api_comprehensive.py",
    "test_stage1_real_api.py",
    "test_tool_errors.py",
    "test_tool_execution.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep network/manual integration checks out of the default CI gate."""
    for item in items:
        relative = item.path.relative_to(item.config.rootpath).as_posix()
        if relative.removeprefix("tests/") in _LIVE_MODULES:
            item.add_marker(pytest.mark.live)


class MockProvider:
    """Mock provider for testing."""

    def __init__(self, model: str = "mock") -> None:
        self._model = model
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        """Mock streaming response."""
        self.calls.append({"messages": messages})

        # Simple mock response
        yield {"type": "content_block_start", "content_block": {"type": "text"}}
        yield {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "mock response"}}
        yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 5}}

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
    ) -> int:
        return sum(len(str(m)) for m in messages) // 4


class ScriptedProvider(Provider):
    """Provider double that returns one pre-scripted stream per request."""

    def __init__(self, scripts: list[list[Any]], model: str = "scripted") -> None:
        self._scripts = [list(script) for script in scripts]
        self._model = model
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "max_tokens": max_tokens,
                "system": system,
                "tools": tools,
            }
        )
        if not self._scripts:
            raise ProviderError("script exhausted")
        for item in self._scripts.pop(0):
            if isinstance(item, BaseException):
                raise item
            yield item

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return sum(len(str(message)) for message in messages) // 4

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class StubCompactor:
    """Deterministic context seam used by recovery tests."""

    def __init__(self, provider: Any, config: Any, keep: int = 8) -> None:
        self.provider = provider
        self.config = config
        self.keep = keep
        self.compactions = 0
        self.given: list[list[dict[str, Any]]] = []

    async def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot = [dict(message) for message in messages]
        self.given.append(snapshot)
        self.compactions += 1
        # Keep enough recent history for the retry assertions while preserving
        # the user/tool protocol pair.  In the real manager this is where the
        # context pyramid decides which layers to fire.
        if len(messages) <= self.keep:
            return messages
        return messages[-self.keep :]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a test workspace with a sample file."""
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def compression_manager(workspace: Path) -> CompressionManager:
    """Create a compression manager for testing."""
    budget = TokenBudget(max_tokens=100_000)
    return CompressionManager(
        workspace=workspace,
        budget=budget,
    )


@pytest.fixture
def tool_ctx(workspace: Path) -> ToolContext:
    """Tool context rooted at the isolated test workspace."""
    return ToolContext(workspace=workspace, shell="gitbash")


@pytest.fixture
def config() -> Any:
    """Minimal configuration object accepted by the compactor seam."""
    return object()


@pytest.fixture
def make_loop(workspace: Path):
    """Build the production StreamingQueryLoop with deterministic tools."""

    def factory(
        scripts: list[list[Any]],
        *,
        context: Any | None = None,
        max_turns: int | None = None,
    ) -> tuple[StreamingQueryLoop, ScriptedProvider]:
        provider = ScriptedProvider(scripts)

        async def run_tools(calls: list[Any]) -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for call in calls:
                path = workspace / str(call.arguments.get("path", ""))
                try:
                    resolved = path.resolve()
                    resolved.relative_to(workspace.resolve())
                    content = resolved.read_text(encoding="utf-8")
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": content,
                            "_tool_name": call.name,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": str(exc),
                            "is_error": True,
                            "_tool_name": call.name,
                        }
                    )
            return results

        return (
            StreamingQueryLoop(
                provider,
                workspace,
                tool_runner=run_tools,
                context=context,
                max_turns=max_turns,
            ),
            provider,
        )

    return factory


async def drain(loop: StreamingQueryLoop, messages: list[dict[str, Any]]) -> list[Any]:
    """Collect the complete typed event stream from a loop run."""
    return [event async for event in loop.run(messages)]


def final_done(events: list[Any]) -> Any:
    """Return the terminal event and fail if the loop did not terminate."""
    from carbide.events import Done

    done = [event for event in events if isinstance(event, Done)]
    assert len(done) == 1, f"expected exactly one Done event, got {done!r}"
    return done[0]


@pytest.fixture(autouse=True)
async def close_bash_sessions_after_test() -> AsyncIterator[None]:
    """Close cached subprocesses before pytest closes the event loop."""
    yield

    from carbide.tools.shell import BashTool

    await BashTool.close_sessions()
