"""Integration tests for the complete system."""

import pytest

from carbide.engine.loop import QueryLoop
from carbide.providers.base import Provider, StreamChunk
from carbide.sdk import Agent
from carbide.tools.defaults import create_default_registry


class MockProvider(Provider):
    """Mock provider for testing."""

    def __init__(self, response_chunks: list[StreamChunk] | None = None):
        self.response_chunks = response_chunks or [
            StreamChunk(type="content_block_start", content=""),
            StreamChunk(type="content_block_delta", content="Hello, "),
            StreamChunk(type="content_block_delta", content="world!"),
            StreamChunk(type="content_block_stop"),
            StreamChunk(type="message_stop"),
        ]
        self.call_count = 0
        self.seen_tools: list[dict] | None = None

    async def stream(
        self, messages, system=None, tools=None, max_tokens=4096, temperature=1.0, **kwargs
    ):
        self.call_count += 1
        self.seen_tools = tools
        for chunk in self.response_chunks:
            yield chunk

    async def count_tokens(self, messages, system=None, tools=None):
        # Simple mock: count chars / 4
        total = sum(len(str(m)) for m in messages)
        if system:
            total += len(str(system))
        return total // 4

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


class TestQueryLoop:
    """Test query loop integration."""

    @pytest.mark.asyncio
    async def test_basic_query(self):
        """Test basic query flow."""
        provider = MockProvider()
        loop = QueryLoop(provider=provider)

        events = []
        async for event in loop.query("Hello"):
            events.append(event)

        # Should have chunk events + turn_complete
        assert len(events) > 0
        assert any(e.type == "chunk" for e in events)
        assert any(e.type == "turn_complete" for e in events)

        # Should have called provider
        assert provider.call_count == 1

        # Should have messages in state
        state = loop.get_state()
        assert state.turn_count == 1
        assert len(state.messages) == 2  # user + assistant

    @pytest.mark.asyncio
    async def test_multiple_turns(self):
        """Test multiple query turns."""
        provider = MockProvider()
        loop = QueryLoop(provider=provider)

        # First turn
        events1 = []
        async for event in loop.query("First message"):
            events1.append(event)

        # Second turn
        events2 = []
        async for event in loop.query("Second message"):
            events2.append(event)

        # State should accumulate
        state = loop.get_state()
        assert state.turn_count == 2
        assert len(state.messages) == 4  # 2 user + 2 assistant

    @pytest.mark.asyncio
    async def test_reset(self):
        """Test reset clears state."""
        provider = MockProvider()
        loop = QueryLoop(provider=provider)

        # Do a query
        async for _event in loop.query("Test"):
            pass

        # Reset
        loop.reset()

        # State should be cleared
        state = loop.get_state()
        assert state.turn_count == 0
        assert len(state.messages) == 0


class TestAgent:
    """Test Agent SDK."""

    @pytest.mark.asyncio
    async def test_agent_query_uses_registered_skill_tool(self, tmp_path):
        """Agent.query should expose load_skill when callers omit tools."""
        skills_dir = tmp_path / "agent-skills"
        skills_dir.mkdir()
        (skills_dir / "review.md").write_text(
            "---\nname: review\ndescription: Review code\n---\n\nInstructions."
        )
        provider = MockProvider()
        registry = create_default_registry()
        agent = Agent(
            provider=provider,
            project_dir=tmp_path,
            tool_registry=registry,
            agent_skills_dir=skills_dir,
            enable_memory=False,
            enable_rules=False,
            enable_compression=False,
        )

        async for _event in agent.query("Review this change"):
            pass

        assert {tool["name"] for tool in provider.seen_tools or []} >= {"load_skill"}

    @pytest.mark.asyncio
    async def test_agent_creation(self, tmp_path):
        """Test creating an agent."""
        # Use mock provider to avoid needing API key
        provider = MockProvider()
        registry = create_default_registry()
        agent = Agent(
            provider=provider,
            project_dir=tmp_path,
            tool_registry=registry,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
        )

        assert agent.provider is not None
        assert agent.loop is not None

    @pytest.mark.asyncio
    async def test_agent_query(self, tmp_path):
        """Test agent query."""
        # Create agent with mock provider
        provider = MockProvider()
        agent = Agent(
            provider=provider,
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
        )

        # Query
        events = []
        async for event in agent.query("Test message"):
            events.append(event)

        assert len(events) > 0
        assert provider.call_count == 1

    @pytest.mark.asyncio
    async def test_agent_stats(self, tmp_path):
        """Test agent statistics."""
        provider = MockProvider()
        agent = Agent(
            provider=provider,
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
        )

        # Initial stats
        stats = agent.get_stats()
        assert stats["turn_count"] == 0
        assert stats["message_count"] == 0

        # After query
        async for _event in agent.query("Test"):
            pass

        stats = agent.get_stats()
        assert stats["turn_count"] == 1
        assert stats["message_count"] == 2

    @pytest.mark.asyncio
    async def test_agent_reset(self, tmp_path):
        """Test agent reset."""
        provider = MockProvider()
        agent = Agent(
            provider=provider,
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_rules=False,
            enable_compression=False,
        )

        # Do a query
        async for _event in agent.query("Test"):
            pass

        # Reset
        agent.reset()

        # Stats should be reset
        stats = agent.get_stats()
        assert stats["turn_count"] == 0
        assert stats["message_count"] == 0

    @pytest.mark.asyncio
    async def test_agent_with_subsystems(self, tmp_path):
        """Test agent subsystems and keep Agent skills outside the workspace."""
        # Memory and rules belong to the project. Agent skills use an explicit
        # user-level directory so a project cannot silently change capabilities.
        (tmp_path / ".carbide" / "memory").mkdir(parents=True)
        (tmp_path / ".carbide" / "rules").mkdir(parents=True)
        agent_skills_dir = tmp_path / "agent-skills"
        agent_skills_dir.mkdir()
        (agent_skills_dir / "global.md").write_text(
            "---\nname: global\ndescription: Agent capability\n---\n\nInstructions."
        )
        project_skills_dir = tmp_path / ".carbide" / "skills"
        project_skills_dir.mkdir()
        (project_skills_dir / "project.md").write_text(
            "---\nname: project\ndescription: Project-owned content\n---\n\nProject notes."
        )

        provider = MockProvider()
        registry = create_default_registry()
        agent = Agent(
            provider=provider,
            project_dir=tmp_path,
            tool_registry=registry,
            enable_memory=True,
            enable_skills=True,
            agent_skills_dir=agent_skills_dir,
            enable_rules=True,
            enable_compression=False,
        )

        # Should have subsystems initialized
        assert agent.memory_recall is not None
        assert agent.skill_manager is not None
        assert set(agent.skill_manager.registry.skills) == {"global"}
        assert "load_skill" in registry
        assert agent.rule_manager is not None

        # Stats should include subsystem info
        stats = agent.get_stats()
        assert "memory" in stats
        assert "skills" in stats
        assert "rules" in stats
