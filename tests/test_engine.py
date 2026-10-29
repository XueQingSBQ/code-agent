"""Tests for engine layer: state, repair, exits, and loop.

Tests the core query loop and its components.
"""

import asyncio
from pathlib import Path

import pytest

from carbide.engine.exits import ExitHandler, ExitReason, check_exit_conditions
from carbide.engine.loop import (
    ModelResponseChunk,
    QueryExit,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    TurnCompleted,
    TurnStarted,
    query_loop,
)
from carbide.engine.repair import RepairManager, validate_message_invariants
from carbide.engine.state import TurnState


class TestTurnState:
    """Test immutable turn state management."""

    def test_frozen_dataclass(self):
        state = TurnState()

        # Should not be able to mutate directly
        with pytest.raises(Exception):  # FrozenInstanceError
            state.turn_number = 5

    def test_add_message(self):
        state = TurnState()
        msg = {"role": "user", "content": "hello"}

        new_state = state.add_message(msg)

        assert len(new_state.messages) == 1
        assert new_state.messages[0] == msg
        # Original state unchanged
        assert len(state.messages) == 0

    def test_increment_turn(self):
        state = TurnState(turn_number=5)

        new_state = state.increment_turn()

        assert new_state.turn_number == 6
        assert state.turn_number == 5  # Original unchanged

    def test_update_tokens(self):
        state = TurnState()

        new_state = state.update_tokens(input_tokens=100, output_tokens=50)

        assert new_state.total_input_tokens == 100
        assert new_state.total_output_tokens == 50

    def test_pending_tool_use_tracking(self):
        state = TurnState()

        # Mark as pending
        state = state.mark_pending_tool_use("tool_123")
        assert "tool_123" in state.pending_tool_use_ids

        # Mark as completed
        state = state.mark_tool_use_completed("tool_123")
        assert "tool_123" not in state.pending_tool_use_ids

    def test_serialization_roundtrip(self):
        state = TurnState(turn_number=5, total_input_tokens=100)
        state = state.add_message({"role": "user", "content": "test"})

        # Serialize
        state_dict = state.to_dict()
        assert state_dict["turn_number"] == 5
        assert state_dict["total_input_tokens"] == 100

        # Deserialize
        restored = TurnState.from_dict(state_dict, list(state.messages))
        assert restored.turn_number == 5
        assert restored.total_input_tokens == 100
        assert len(restored.messages) == 1


class TestRepairManager:
    """Test conversation repair mechanisms."""

    def test_find_orphan_tool_uses(self):
        manager = RepairManager()

        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool_1", "name": "read"},
                    {"type": "tool_use", "id": "tool_2", "name": "write"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"}
                ],
            },
            # tool_2 has no result - orphan!
        ]

        orphans = manager.find_orphan_tool_uses(messages)

        assert len(orphans) == 1
        assert "tool_2" in orphans

    def test_yield_missing_tool_results(self):
        manager = RepairManager()

        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool_1", "name": "bash"}],
            }
        ]

        synthetic = manager.yield_missing_tool_results(messages, ["tool_1"])

        assert len(synthetic) == 1
        result_msg = synthetic[0]
        assert result_msg["role"] == "user"

        result_block = result_msg["content"][0]
        assert result_block["type"] == "tool_result"
        assert result_block["tool_use_id"] == "tool_1"
        assert result_block["is_error"]
        assert result_block["_synthetic"]

    def test_detect_truncation(self):
        manager = RepairManager()

        response = {"stop_reason": "max_tokens"}

        is_truncated, reason = manager.detect_truncation(response)

        assert is_truncated
        assert reason == "max_tokens"

    def test_handle_truncation_escalation(self):
        manager = RepairManager()

        # First truncation: silent retry
        should_retry, nudge = manager.handle_truncation(1, [])
        assert should_retry
        assert nudge is None

        # Second truncation: nudge
        should_retry, nudge = manager.handle_truncation(2, [])
        assert should_retry
        assert nudge is not None
        assert nudge[0]["_truncation_nudge"]

        # Third truncation: give up
        should_retry, nudge = manager.handle_truncation(3, [])
        assert not should_retry

    def test_repair_conversation(self):
        manager = RepairManager()

        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "orphan_1", "name": "read"}],
            }
        ]

        repaired, report = manager.repair_conversation(messages)

        assert report["orphans_fixed"] == 1
        assert len(repaired) > len(messages)

        # Check synthetic result was added
        last_msg = repaired[-1]
        assert last_msg["role"] == "user"
        assert last_msg["content"][0]["tool_use_id"] == "orphan_1"

    def test_validate_message_invariants(self):
        # Valid conversation
        valid = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool_1", "name": "read"}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"}
                ],
            },
        ]

        violations = validate_message_invariants(valid)
        assert len(violations) == 0

        # Invalid: orphan tool_use
        invalid = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool_1", "name": "read"}],
            }
        ]

        violations = validate_message_invariants(invalid)
        assert len(violations) == 1
        assert "Orphan tool_use" in violations[0]


class TestExitHandler:
    """Test exit handling and cleanup."""

    def test_exit_messages(self):
        handler = ExitHandler()

        msg = handler._get_exit_message(ExitReason.TASK_COMPLETE)
        assert "success" in msg.lower()

        msg = handler._get_exit_message(ExitReason.API_ERROR)
        assert "error" in msg.lower()

    def test_should_save_state(self):
        handler = ExitHandler()

        # Should save for normal completion
        assert handler._should_save_state(ExitReason.TASK_COMPLETE)

        # Should not save for safety stop
        assert not handler._should_save_state(ExitReason.SAFETY_STOP)

    def test_is_error_exit(self):
        handler = ExitHandler()

        assert handler._is_error_exit(ExitReason.API_ERROR)
        assert not handler._is_error_exit(ExitReason.TASK_COMPLETE)

    @pytest.mark.asyncio
    async def test_cleanup_handler(self):
        handler = ExitHandler()
        cleanup_called = []

        def cleanup(ctx):
            cleanup_called.append(True)
            return {"cleaned": True}

        handler.register_cleanup(ExitReason.TASK_COMPLETE, cleanup)

        report = await handler.handle_exit(ExitReason.TASK_COMPLETE, {})

        assert len(cleanup_called) == 1
        assert report["cleanup"]["cleaned"]

    def test_create_exit_summary(self):
        handler = ExitHandler()
        state = TurnState(
            turn_number=10,
            total_input_tokens=1000,
            total_output_tokens=500,
            total_tool_calls=20,
            failed_tool_calls=2,
        )

        summary = handler.create_exit_summary(ExitReason.TASK_COMPLETE, state)

        assert summary["exit_reason"] == "task_complete"
        assert summary["statistics"]["total_turns"] == 10
        assert summary["statistics"]["total_tokens"] == 1500
        assert summary["statistics"]["total_tool_calls"] == 20
        assert summary["resumable"]

    def test_check_exit_conditions(self):
        # No exit
        state = TurnState()
        assert check_exit_conditions(state) is None

        # Explicit exit flag
        state = state.mark_exit("user_request")
        assert check_exit_conditions(state) == ExitReason.USER_REQUEST

        # Turn limit
        state = TurnState(turn_number=101)
        assert check_exit_conditions(state) == ExitReason.TURN_LIMIT

        # Failure threshold
        state = TurnState(total_tool_calls=20, failed_tool_calls=15)
        assert check_exit_conditions(state) == ExitReason.FAILURE_THRESHOLD


class TestQueryLoop:
    """Test main query loop."""

    @pytest.mark.asyncio
    async def test_basic_query_loop(self):
        """Test simple query with no tool use."""

        async def mock_stream_model(messages):
            # Yield text response
            yield {"type": "content_block_start", "content_block": {"type": "text"}}
            yield {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            }
            yield {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
            }
            yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 5}}

        async def mock_execute_tools(tool_uses, state):
            return []

        initial_state = TurnState(workspace=Path.cwd())
        events = []

        async for event in query_loop(
            initial_state,
            "Hello",
            mock_stream_model,
            mock_execute_tools,
        ):
            events.append(event)

        # Check event sequence
        assert any(isinstance(e, TurnStarted) for e in events)
        assert any(isinstance(e, ModelResponseChunk) for e in events)
        assert any(isinstance(e, TurnCompleted) for e in events)
        assert any(isinstance(e, QueryExit) for e in events)

        # Check exit
        exit_event = [e for e in events if isinstance(e, QueryExit)][0]
        assert exit_event.reason == ExitReason.TASK_COMPLETE

    @pytest.mark.asyncio
    async def test_query_loop_with_tool_use(self):
        """Test query with tool execution."""

        async def mock_stream_model(messages):
            # First turn: request tool
            if len(messages) == 1:
                yield {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool_123",
                        "name": "read",
                    },
                }
                yield {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                }
                yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 5}}
            # Second turn: respond after tool
            else:
                yield {"type": "content_block_start", "content_block": {"type": "text"}}
                yield {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Done"},
                }
                yield {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                }
                yield {"type": "message_stop", "usage": {"input_tokens": 20, "output_tokens": 3}}

        async def mock_execute_tools(tool_uses, state):
            return [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_123",
                    "content": "file contents",
                    "_tool_name": "read",
                }
            ]

        initial_state = TurnState(workspace=Path.cwd())
        events = []

        async for event in query_loop(
            initial_state,
            "Read file",
            mock_stream_model,
            mock_execute_tools,
        ):
            events.append(event)

        # Check tool events
        tool_starts = [e for e in events if isinstance(e, ToolExecutionStarted)]
        tool_completions = [e for e in events if isinstance(e, ToolExecutionCompleted)]

        assert len(tool_starts) == 1
        assert tool_starts[0].tool_name == "read"

        assert len(tool_completions) == 1
        assert tool_completions[0].success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
