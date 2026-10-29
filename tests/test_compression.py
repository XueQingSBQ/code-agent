"""Tests for context compression."""

from __future__ import annotations

import pytest

from carbide.execution.compression import (
    AdaptiveCompressor,
    MessageCompressor,
    TokenBudget,
)


def test_token_budget_estimation():
    """Token estimation uses ~4 characters per token."""
    budget = TokenBudget(max_tokens=1000)

    # ~100 characters = ~25 tokens
    text = "a" * 100
    estimated = budget.estimate_tokens(text)
    assert estimated == 25


def test_token_budget_should_compress():
    """Compression triggered at threshold."""
    budget = TokenBudget(max_tokens=100, compression_threshold=0.8)

    # Below threshold (70 tokens including dict overhead)
    small_messages = [{"role": "user", "content": "a" * 240}]  # ~70 tokens
    assert not budget.should_compress(small_messages)

    # Above threshold (90 tokens including dict overhead)
    large_messages = [{"role": "user", "content": "a" * 320}]  # ~90 tokens
    assert budget.should_compress(large_messages)


def test_token_budget_usage_ratio():
    """Usage ratio calculated correctly."""
    budget = TokenBudget(max_tokens=100)

    messages = [{"role": "user", "content": "a" * 200}]  # ~50 tokens
    budget.should_compress(messages)

    # Allow some variance due to dict overhead
    ratio = budget.get_usage_ratio()
    assert 0.4 < ratio < 0.7  # Should be around 0.5-0.6


def test_message_compressor_keeps_edges():
    """Compressor keeps first and last messages."""
    compressor = MessageCompressor(keep_first=2, keep_last=2)

    messages = [
        {"role": "system", "content": "msg1"},
        {"role": "user", "content": "msg2"},
        {"role": "assistant", "content": "msg3"},
        {"role": "user", "content": "msg4"},
        {"role": "assistant", "content": "msg5"},
        {"role": "user", "content": "msg6"},
    ]

    compressed = compressor.compress(messages)

    # Should have: 2 first + summary + 2 last
    assert len(compressed) == 5
    assert compressed[0]["content"] == "msg1"
    assert compressed[1]["content"] == "msg2"
    assert compressed[-2]["content"] == "msg5"
    assert compressed[-1]["content"] == "msg6"
    assert "compressed" in compressed[2]["content"][0]["text"].lower()


def test_message_compressor_no_compression_needed():
    """No compression when messages fit within limits."""
    compressor = MessageCompressor(keep_first=2, keep_last=2)

    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
    ]

    compressed = compressor.compress(messages)

    # Not enough messages to compress (3 <= 2+2)
    assert len(compressed) == 3
    assert compressed == messages


def test_message_compressor_counts_tool_calls():
    """Compressor counts tool calls in summary."""
    compressor = MessageCompressor(keep_first=1, keep_last=1)

    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "bash", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "1", "content": "output"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "read", "input": {}}],
        },
        {"role": "user", "content": "final message"},
    ]

    compressed = compressor.compress(messages)

    # Check summary contains counts
    summary = compressed[1]
    summary_text = summary["content"][0]["text"]
    assert "2 tool calls" in summary_text
    assert "1 results" in summary_text


def test_message_compressor_preserves_system_messages():
    """System messages in middle section are preserved."""
    compressor = MessageCompressor(keep_first=1, keep_last=1, preserve_system=True)

    messages = [
        {"role": "user", "content": "first"},
        {"role": "system", "content": "important system message"},
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "last"},
    ]

    compressed = compressor.compress(messages)

    # Should preserve system message
    system_messages = [msg for msg in compressed if msg.get("role") == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == "important system message"


def test_message_compressor_no_preserve_system():
    """System messages can be compressed if not preserved."""
    compressor = MessageCompressor(keep_first=1, keep_last=1, preserve_system=False)

    messages = [
        {"role": "user", "content": "first"},
        {"role": "system", "content": "system message"},
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "last"},
    ]

    compressed = compressor.compress(messages)

    # System message should not be preserved
    system_messages = [msg for msg in compressed if msg.get("role") == "system"]
    assert len(system_messages) == 0


def test_compression_ratio():
    """Compression ratio calculated correctly."""
    compressor = MessageCompressor(keep_first=1, keep_last=1)

    original = [
        {"role": "user", "content": "a" * 100},
        {"role": "assistant", "content": "b" * 100},
        {"role": "user", "content": "c" * 100},
    ]

    compressed = compressor.compress(original)

    ratio = compressor.get_compression_ratio(original, compressed)

    # Compressed should be smaller
    assert ratio < 1.0


def test_adaptive_compressor_compresses_when_needed():
    """Adaptive compressor uses budget to decide compression."""
    budget = TokenBudget(max_tokens=100, compression_threshold=0.5)
    adaptive = AdaptiveCompressor(budget)

    # Large messages (over threshold) - need enough to compress
    large_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a" * 400},
        {"role": "assistant", "content": "b" * 400},
        {"role": "user", "content": "c" * 400},
        {"role": "assistant", "content": "d" * 400},
        {"role": "user", "content": "e" * 400},
    ]  # Many messages to enable compression

    compressed, was_compressed = adaptive.compress_if_needed(large_messages)

    assert was_compressed
    # Should compress middle section
    assert len(compressed) <= len(large_messages)


def test_adaptive_compressor_skips_when_not_needed():
    """Adaptive compressor skips compression when under budget."""
    budget = TokenBudget(max_tokens=1000, compression_threshold=0.8)
    adaptive = AdaptiveCompressor(budget)

    # Small messages (under threshold)
    small_messages = [{"role": "user", "content": "small"}] * 3

    compressed, was_compressed = adaptive.compress_if_needed(small_messages)

    assert not was_compressed
    assert compressed == small_messages


def test_adaptive_compressor_adjusts_strategy():
    """Adaptive compressor adjusts strategy based on usage."""
    budget = TokenBudget(max_tokens=1000)
    adaptive = AdaptiveCompressor(budget)

    # Low usage - light compression
    adaptive.adjust_strategy(0.7)
    assert adaptive.compressor.keep_first == 3
    assert adaptive.compressor.keep_last == 7

    # Medium usage - standard compression
    adaptive.adjust_strategy(0.85)
    assert adaptive.compressor.keep_first == 2
    assert adaptive.compressor.keep_last == 5

    # High usage - aggressive compression
    adaptive.adjust_strategy(0.95)
    assert adaptive.compressor.keep_first == 1
    assert adaptive.compressor.keep_last == 3


def test_empty_messages():
    """Compressor handles empty message list."""
    compressor = MessageCompressor()

    compressed = compressor.compress([])

    assert compressed == []


def test_compression_preserves_structure():
    """Compressed messages maintain proper structure."""
    compressor = MessageCompressor(keep_first=1, keep_last=1)

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "middle"}]},
        {"role": "user", "content": [{"type": "text", "text": "last"}]},
    ]

    compressed = compressor.compress(messages)

    # All messages should have role and content
    for msg in compressed:
        assert "role" in msg
        assert "content" in msg
