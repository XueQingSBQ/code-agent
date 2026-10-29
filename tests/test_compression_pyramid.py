"""Tests for five-layer compression pyramid.

Tests each layer independently and then the full pyramid integration.
"""

import asyncio
from pathlib import Path

import pytest

from carbide.context.budget import TokenBudget, TokenCalibrator
from carbide.context.compact import CompactCompressor
from carbide.context.manager import CompressionManager, LayerBus
from carbide.context.micro import MicroCompressor
from carbide.context.restore import RestoreCompressor
from carbide.context.snip import SnipCompressor
from carbide.context.spill import SpillCompressor


class TestTokenBudget:
    """Test token budget and three-tier calibration."""

    def test_basic_estimation(self):
        budget = TokenBudget(max_tokens=100_000)

        # Test string estimation (~4 chars/token)
        estimate = budget.estimate_tokens("hello world")
        assert estimate.tokens == 2  # 11 chars / 4 ≈ 2

    def test_calibration_updates_coefficient(self):
        budget = TokenBudget()

        # Simulate API calibration
        content = "a" * 400  # 400 chars
        actual_tokens = 120  # API says 120 tokens (more than 400/4=100)

        budget.update_from_api_count(actual_tokens, content)

        # Coefficient should be adjusted
        assert budget.calibration.coefficient > 1.0
        assert budget.calibration.sample_count == 1

    def test_usage_tracking(self):
        budget = TokenBudget(max_tokens=1000)

        budget.update_from_usage(500)
        assert budget.get_usage_ratio() == 0.5
        assert budget.get_remaining_tokens() == 500

    def test_should_compress_threshold(self):
        budget = TokenBudget(max_tokens=1000, compression_threshold=0.8)

        messages = [{"content": "x" * 4000}]  # ~1000 tokens
        assert budget.should_compress(messages)


class TestSpillCompressor:
    """Test L1: Spill large results to disk."""

    def test_spill_large_result(self, tmp_path):
        spill_dir = tmp_path / "spill"
        compressor = SpillCompressor(
            spill_dir=spill_dir,
            single_result_threshold=100,
            preview_size=50  # Small preview to ensure tokens freed
        )

        # Create message with large tool result
        large_content = "x" * 200  # Exceeds threshold
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "test_123",
                        "content": large_content,
                    }
                ],
            }
        ]

        compressed, result = compressor.compress(messages)

        assert result.spilled
        assert result.tokens_freed > 0
        assert result.spill_path is not None
        assert result.spill_path.exists()

        # Check preview was created
        preview_block = compressed[0]["content"][0]
        assert preview_block["_spilled"]
        assert len(preview_block["content"]) < len(large_content)

    def test_no_spill_small_result(self, tmp_path):
        spill_dir = tmp_path / "spill"
        compressor = SpillCompressor(spill_dir=spill_dir, single_result_threshold=1000)

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "test_123",
                        "content": "small",
                    }
                ],
            }
        ]

        compressed, result = compressor.compress(messages)

        assert not result.spilled
        assert result.tokens_freed == 0


class TestSnipCompressor:
    """Test L2: Model-directed snip."""

    def test_mark_and_snip(self):
        compressor = SnipCompressor()

        messages = [
            {"id": "msg1", "role": "user", "content": "hello"},
            {"id": "msg2", "role": "assistant", "content": "hi"},
            {"id": "msg3", "role": "user", "content": "bye"},
        ]

        # Mark msg2 for deletion
        compressor.mark_for_snip("msg2")

        compressed, result = compressor.compress(messages)

        assert result.snipped
        assert result.messages_removed == 1
        assert len(compressed) == 2
        assert compressed[0]["id"] == "msg1"
        assert compressed[1]["id"] == "msg3"

    def test_no_snip_when_empty(self):
        compressor = SnipCompressor()

        messages = [{"id": "msg1", "role": "user", "content": "hello"}]

        compressed, result = compressor.compress(messages)

        assert not result.snipped
        assert result.messages_removed == 0


class TestMicroCompressor:
    """Test L3: Time-based micro-compaction."""

    def test_should_not_compact_before_threshold(self):
        compressor = MicroCompressor(time_threshold_seconds=60)
        compressor.update_call_time()

        assert not compressor.should_compact()

    def test_clears_refetchable_results(self):
        compressor = MicroCompressor(time_threshold_seconds=0, keep_recent=2)  # Keep only 2 recent

        # Set last_call_time to past to trigger compaction
        import time
        compressor.last_call_time = time.time() - 1  # 1 second ago

        # Create multiple tool results so some are not "recent"
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read_1",
                        "content": "old file 1",
                        "_tool_name": "read",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read_2",
                        "content": "old file 2",
                        "_tool_name": "read",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read_3",
                        "content": "recent file",
                        "_tool_name": "read",
                    }
                ],
            },
        ]

        compressed, result = compressor.compress(messages)

        assert result.compacted
        assert result.messages_cleared == 1  # Should clear the oldest one
        assert result.preserved_count == 2  # Keep 2 recent

        # Check first message was cleared
        cleared_block = compressed[0]["content"][0]
        assert cleared_block["_micro_cleared"]
        assert "cleared" in cleared_block["content"].lower()

        # Check recent messages preserved
        assert compressed[1]["content"][0]["content"] == "old file 2"
        assert compressed[2]["content"][0]["content"] == "recent file"

    def test_preserves_non_reproducible(self):
        compressor = MicroCompressor(time_threshold_seconds=0)
        compressor.update_call_time()

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "agent_123",
                        "content": "agent output",
                        "_tool_name": "agent",
                    }
                ],
            }
        ]

        compressed, result = compressor.compress(messages)

        # Should not clear agent results
        assert result.messages_cleared == 0
        assert compressed[0]["content"][0]["content"] == "agent output"


class TestCompactCompressor:
    """Test L4: Full compaction."""

    @pytest.mark.asyncio
    async def test_full_compaction(self):
        compressor = CompactCompressor(trigger_threshold=100_000)

        messages = [
            {"role": "user", "content": "task 1"},
            {"role": "assistant", "content": "done 1"},
            {"role": "user", "content": "task 2 " + "detail " * 200},
            {"role": "assistant", "content": "done 2"},
            {"role": "user", "content": "task 3 " + "detail " * 200},
            {"role": "assistant", "content": "done 3"},
            {"role": "user", "content": "task 4"},  # Extra messages
            {"role": "assistant", "content": "done 4"},
        ]

        async def mock_summarize(msgs, prompt):
            return """1. Task Objective: Complete tasks
2. Key Decisions: None
3. Files Modified: None
4. Active Problems: None
5. Completed Sub-tasks: tasks 1-4
6. Important Tool Results: None
7. Error Patterns: None
8. User Preferences: None
9. Next Steps: None"""

        compressed, result = await compressor.compress(messages, mock_summarize)

        assert result.compacted
        assert result.segments_created > 0
        assert len(compressed) < len(messages)

        # Retain both recent user turns, rather than splitting the last turn.
        assert len(compressed) == 7


class TestRestoreCompressor:
    """Test L4.5: Post-compact restoration."""

    @pytest.mark.asyncio
    async def test_restore_files(self, tmp_path):
        # Create test files
        file1 = tmp_path / "test1.py"
        file1.write_text("def foo(): pass")

        compressor = RestoreCompressor(workspace=tmp_path, max_files=2)

        # Simulate post-compact messages
        messages = [
            {"role": "assistant", "content": "summary", "_l4_summary": True},
            {"role": "user", "content": "continue"},
        ]

        restored, result = await compressor.restore(
            messages,
            recently_accessed_files=[file1],
        )

        assert result.restored
        assert result.files_restored == 1
        assert len(restored) > len(messages)

        # Check file was restored
        last_msg = restored[-1]
        assert "_l45_restored_file" in last_msg


class TestLayerBus:
    """Test inter-layer communication bus."""

    def test_reports_and_totals(self):
        bus = LayerBus()

        bus.report("L1", 1000)
        bus.report("L2", 500)

        assert bus.get_total_freed() == 1500
        assert bus.get_layer_contribution("L1") == 1000
        assert bus.get_layer_contribution("L2") == 500

    def test_should_skip_layer(self):
        bus = LayerBus()

        bus.report("L1", 1000)

        assert bus.should_skip_layer(500)  # Already freed enough
        assert not bus.should_skip_layer(1500)  # Need more


class TestCompressionManager:
    """Test full pyramid integration."""

    @pytest.mark.asyncio
    async def test_manager_runs_layers_in_sequence(self, tmp_path):
        budget = TokenBudget(max_tokens=10_000, compression_threshold=0.5)
        manager = CompressionManager(
            workspace=tmp_path,
            budget=budget,
            spill_dir=tmp_path / "spill",
        )

        # Create messages that trigger compression
        large_content = "x" * 60_000  # Large enough to trigger spill
        messages = [
            {"role": "user", "content": "start"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "test_123",
                        "content": large_content,
                    }
                ],
            },
        ]

        compressed, result = await manager.compress(messages)

        assert result.compressed
        assert "L1:spill" in result.layers_triggered
        assert result.total_tokens_freed > 0

    @pytest.mark.asyncio
    async def test_manager_early_bailout(self, tmp_path):
        budget = TokenBudget(max_tokens=10_000)
        manager = CompressionManager(workspace=tmp_path, budget=budget)

        # Small messages, won't trigger compression
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

        compressed, result = await manager.compress(messages)

        # Should not compress small messages
        assert len(result.layers_triggered) == 0


class TestSnipTool:
    """Test snip tool integration."""

    @pytest.mark.asyncio
    async def test_snip_tool_marks_messages(self):
        from carbide.tools.snip import SnipTool, SnipParams

        compressor = SnipCompressor()
        tool = SnipTool(snip_compressor=compressor)

        params = SnipParams(
            message_ids=["msg1", "msg2"],
            reason="too old",
        )

        result = await tool.run(params, None)

        assert not result.is_error
        assert "2 message(s)" in result.content

        # Check messages were marked
        pending = compressor.get_pending_snips()
        assert "msg1" in pending
        assert "msg2" in pending


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
