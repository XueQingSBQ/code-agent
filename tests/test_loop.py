"""Query loop behaviour, driven by a scripted provider.

The happy path gets one test. The rest of this file is failure handling, which
is where the loop's value actually is.
"""

from __future__ import annotations

from typing import Any

from carbide.events import (
    ExitReason,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    Warning,
)
from carbide.providers.base import (
    PromptTooLongError,
    ProviderError,
    StreamEnd,
    StreamTextDelta,
    StreamToolUse,
    Usage,
)
from tests.conftest import StubCompactor, drain, final_done


def _text_turn(text: str, *, stop: str = "end_turn") -> list[Any]:
    return [
        StreamTextDelta(text),
        StreamEnd(
            stop_reason=stop,  # type: ignore[arg-type]
            usage=Usage(input_tokens=10, output_tokens=5),
            assistant_content=[{"type": "text", "text": text}],
        ),
    ]


def _tool_turn(name: str, arguments: dict[str, Any], *, call_id: str = "toolu_1") -> list[Any]:
    return [
        StreamTextDelta("working on it"),
        StreamToolUse(id=call_id, name=name, arguments=arguments),
        StreamEnd(
            stop_reason="tool_use",
            usage=Usage(input_tokens=20, output_tokens=8),
            assistant_content=[
                {"type": "text", "text": "working on it"},
                {"type": "tool_use", "id": call_id, "name": name, "input": arguments},
            ],
        ),
    ]


def _user(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


async def test_no_tool_call_ends_the_turn(make_loop: Any) -> None:
    loop, provider = make_loop([_text_turn("done")])
    events = await drain(loop, _user("hi"))

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["done"]
    assert final_done(events).reason is ExitReason.COMPLETED
    assert provider.call_count == 1


async def test_tool_call_continues_then_stops(make_loop: Any, workspace: Any) -> None:
    loop, provider = make_loop(
        [_tool_turn("read", {"path": "hello.py"}), _text_turn("the file prints hi")]
    )
    events = await drain(loop, _user("what does hello.py do"))

    started = [e for e in events if isinstance(e, ToolCallStarted)]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.name for e in started] == ["read"]
    assert len(finished) == 1 and not finished[0].is_error
    assert "print('hi')" in finished[0].preview

    assert provider.call_count == 2
    # The second request carries the assistant turn and its tool_result.
    second = provider.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "toolu_1"

    turns = [e for e in events if isinstance(e, TurnCompleted)]
    assert [t.turn for t in turns] == [1, 2]
    assert loop.final_messages[-1]["content"][0]["type"] == "text"


async def test_tool_dispatches_before_the_stream_ends(make_loop: Any) -> None:
    """ToolCallStarted must be emitted before the turn's final event, not after.

    That ordering is the observable proof that tools are dispatched from the
    stream rather than after it.
    """
    loop, _ = make_loop([_tool_turn("read", {"path": "hello.py"}), _text_turn("ok")])
    events = await drain(loop, _user("read it"))

    kinds = [type(e).__name__ for e in events]
    assert kinds.index("ToolCallStarted") < kinds.index("ToolCallFinished")
    assert kinds.index("ToolCallStarted") < kinds.index("TurnCompleted")


async def test_failed_tool_is_reported_and_the_loop_continues(make_loop: Any) -> None:
    loop, provider = make_loop(
        [_tool_turn("read", {"path": "missing.py"}), _text_turn("that file is not there")]
    )
    events = await drain(loop, _user("read missing.py"))

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished[0].is_error
    # The failure is fed back to the model rather than ending the run.
    assert provider.call_count == 2
    result_block = provider.calls[1]["messages"][-1]["content"][0]
    assert result_block["is_error"] is True
    assert final_done(events).reason is ExitReason.COMPLETED


async def test_max_turns_stops_the_loop(make_loop: Any) -> None:
    loop, provider = make_loop(
        [_tool_turn("read", {"path": "hello.py"}, call_id=f"toolu_{i}") for i in range(5)],
        max_turns=2,
    )
    events = await drain(loop, _user("keep reading"))

    assert final_done(events).reason is ExitReason.MAX_TURNS
    assert provider.call_count == 2


async def test_default_turn_limit_is_unbounded(make_loop: Any) -> None:
    """The default loop must not stop a legitimate long tool workflow at ten turns."""
    scripts = [
        _tool_turn("read", {"path": "hello.py"}, call_id=f"toolu_{i}")
        for i in range(11)
    ]
    scripts.append(_text_turn("finished after all tool steps"))

    loop, provider = make_loop(scripts)
    events = await drain(loop, _user("keep working"))

    assert final_done(events).reason is ExitReason.COMPLETED
    assert final_done(events).turns == 12
    assert provider.call_count == 12


async def test_orphan_tool_use_in_incoming_history_is_repaired(make_loop: Any) -> None:
    """A history saved mid-turn (Ctrl+C, crash) must be repaired, not rejected."""
    loop, provider = make_loop([_text_turn("picking up where we left off")])
    broken: list[dict[str, Any]] = [
        {"role": "user", "content": "read the file"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_lost", "name": "read", "input": {"path": "a"}}
            ],
        },
    ]
    events = await drain(loop, broken)

    warnings = [e for e in events if isinstance(e, Warning)]
    assert any(w.code == "orphan_tool_use_repaired" for w in warnings)
    sent = provider.calls[0]["messages"]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"][0]["tool_use_id"] == "toolu_lost"
    assert sent[-1]["content"][0]["is_error"] is True


class TestTruncationRecovery:
    """The escalate -> nudge -> nudge -> give-up ladder.

    Each rung must be reached exactly once. An earlier version reset the counter
    on every turn, so it escalated forever; that is what these tests pin down.
    """

    async def test_first_truncation_escalates_the_token_cap(self, make_loop: Any) -> None:
        loop, provider = make_loop(
            [_text_turn("half an ans", stop="max_tokens"), _text_turn("a full answer")]
        )
        events = await drain(loop, _user("write something long"))

        warnings = [e for e in events if isinstance(e, Warning)]
        assert [w.code for w in warnings] == ["output_escalated"]
        assert provider.calls[0]["max_tokens"] == 8_000
        assert provider.calls[1]["max_tokens"] == 16_000
        assert final_done(events).reason is ExitReason.COMPLETED

    async def test_escalated_retry_does_not_prefill_the_truncated_text(
        self, make_loop: Any
    ) -> None:
        """The retry must not carry the cut-off assistant message.

        Leaving it in place makes the request an assistant prefill, so the model
        resumes mid-sentence instead of answering again with more room.
        """
        loop, provider = make_loop(
            [_text_turn("half an ans", stop="max_tokens"), _text_turn("a full answer")]
        )
        await drain(loop, _user("write something long"))

        retry = provider.calls[1]["messages"]
        assert retry[-1]["role"] == "user"
        assert all(m["role"] != "assistant" for m in retry)

    async def test_second_truncation_nudges_and_keeps_the_partial_text(
        self, make_loop: Any
    ) -> None:
        loop, provider = make_loop(
            [
                _text_turn("part one", stop="max_tokens"),
                _text_turn("part two", stop="max_tokens"),
                _text_turn("part three"),
            ]
        )
        events = await drain(loop, _user("write something long"))

        codes = [e.code for e in events if isinstance(e, Warning)]
        assert codes == ["output_escalated", "output_continuation_nudge"]

        third = provider.calls[2]["messages"]
        # Partial output is kept, and the nudge follows it as a user turn.
        assert any(
            m["role"] == "assistant" and m["content"][0]["text"] == "part two" for m in third
        )
        assert third[-1]["role"] == "user"
        assert "cut off" in str(third[-1]["content"])

    async def test_the_ladder_gives_up_instead_of_looping(self, make_loop: Any) -> None:
        loop, provider = make_loop([_text_turn(f"chunk {i}", stop="max_tokens") for i in range(8)])
        events = await drain(loop, _user("write something long"))

        codes = [e.code for e in events if isinstance(e, Warning)]
        assert codes == [
            "output_escalated",
            "output_continuation_nudge",
            "output_continuation_nudge",
        ]
        assert provider.call_count == 4
        assert final_done(events).reason is ExitReason.OUTPUT_TRUNCATION_UNRECOVERED

    async def test_truncation_with_tool_calls_is_not_a_truncation(self, make_loop: Any) -> None:
        """max_tokens alongside tool calls means the tools still ran.

        Recovery would re-issue the request and duplicate the side effects.
        """
        turn = _tool_turn("read", {"path": "hello.py"})
        turn[-1] = StreamEnd(
            stop_reason="max_tokens",
            usage=Usage(),
            assistant_content=turn[-1].assistant_content,
        )
        loop, provider = make_loop([turn, _text_turn("done")])
        events = await drain(loop, _user("read it"))

        assert [e.code for e in events if isinstance(e, Warning)] == []
        assert provider.calls[1]["max_tokens"] == 8_000


class TestWindowExhaustion:
    """``model_context_window_exceeded``: out of window part-way through a reply.

    Distinct from max_tokens and treated oppositely. Raising the output cap makes
    this worse; only compaction creates room. Before these tests the reason fell
    through to step 6 and the run reported COMPLETED — a truncated answer
    presented as a finished one.
    """

    async def test_it_compacts_and_retries(self, make_loop: Any, config: Any) -> None:
        compactor = StubCompactor(provider=None, config=config)
        loop, provider = make_loop(
            [
                _text_turn("I was explaining the", stop="model_context_window_exceeded"),
                _text_turn("here is the full answer"),
            ],
            context=compactor,
        )
        events = await drain(loop, _user("explain the architecture"))

        codes = [e.code for e in events if isinstance(e, Warning)]
        assert codes == ["window_exceeded_mid_response"]
        assert compactor.compactions == 1
        assert provider.call_count == 2
        assert final_done(events).reason is ExitReason.COMPLETED

    async def test_it_is_never_reported_as_completed(self, make_loop: Any) -> None:
        """With M0's real compactor this must fail loudly, not finish quietly."""
        loop, _ = make_loop([_text_turn("cut off mid-", stop="model_context_window_exceeded")])
        events = await drain(loop, _user("hi"))

        # COMPACTION_FAILED, specifically: the run is over *and* the reason names
        # what went wrong. Reporting COMPLETED here would be the actual bug.
        assert final_done(events).reason is ExitReason.COMPACTION_FAILED

    async def test_recovery_is_bounded(self, make_loop: Any, config: Any) -> None:
        """A run that keeps refilling the window has to stop, not keep summarising.

        The counter is deliberately not reset on a good turn, so this cannot be
        restarted into an endless compact/retry cycle.
        """
        compactor = StubCompactor(provider=None, config=config)
        loop, provider = make_loop(
            [_text_turn("nope", stop="model_context_window_exceeded")] * 4,
            context=compactor,
        )
        events = await drain(loop, _user("hi"))

        codes = [e.code for e in events if isinstance(e, Warning)]
        assert codes == ["window_exceeded_mid_response"] * 2
        assert compactor.compactions == 2
        assert provider.call_count == 3
        assert final_done(events).reason is ExitReason.PROMPT_TOO_LONG

    async def test_tool_results_are_carried_into_the_retry(
        self, make_loop: Any, config: Any, workspace: Any
    ) -> None:
        """A tool that ran had side effects. Dropping its result loses them.

        Worse, the assistant's tool_use block would survive without its result
        and the next request would be rejected outright.
        """
        compactor = StubCompactor(provider=None, config=config, keep=4)
        loop, _ = make_loop(
            [
                [
                    StreamToolUse(id="toolu_1", name="read", arguments={"path": "hello.py"}),
                    StreamEnd(
                        stop_reason="model_context_window_exceeded",
                        usage=Usage(input_tokens=199_000, output_tokens=4_000),
                        assistant_content=[
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "read",
                                "input": {"path": "hello.py"},
                            }
                        ],
                    ),
                ],
                _text_turn("the file prints hi"),
            ],
            context=compactor,
        )
        events = await drain(loop, _user("read hello.py"))

        assert any(isinstance(e, ToolCallFinished) for e in events)
        handed_over = compactor.given[0]
        blocks = [
            block
            for message in handed_over
            if isinstance(message["content"], list)
            for block in message["content"]
        ]
        assert [b["id"] for b in blocks if b["type"] == "tool_use"] == ["toolu_1"]
        assert [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"] == ["toolu_1"]
        assert final_done(events).reason is ExitReason.COMPLETED

    async def test_a_bare_partial_reply_is_not_carried_over(
        self, make_loop: Any, config: Any
    ) -> None:
        """Nothing ran, so there is nothing to keep — and keeping it would prefill.

        A trailing assistant message turns the retry into a continuation, so the
        model would resume mid-sentence instead of answering afresh.
        """
        compactor = StubCompactor(provider=None, config=config, keep=4)
        loop, _ = make_loop(
            [
                _text_turn("I was in the middle of", stop="model_context_window_exceeded"),
                _text_turn("done"),
            ],
            context=compactor,
        )
        await drain(loop, _user("hi"))

        handed_over = compactor.given[0]
        assert all(m["role"] != "assistant" for m in handed_over)
        assert "I was in the middle of" not in str(handed_over)


class TestProviderFailures:
    async def test_prompt_too_long_triggers_reactive_compaction(self, make_loop: Any) -> None:
        """M0 has no compactor, so this must surface as COMPACTION_FAILED.

        The point is that the reactive path is wired and reported, not silently
        swallowed into a generic error.
        """
        loop, provider = make_loop([[PromptTooLongError()]])
        events = await drain(loop, _user("a very long conversation"))

        codes = [e.code for e in events if isinstance(e, Warning)]
        assert codes == ["reactive_compact"]
        assert final_done(events).reason is ExitReason.COMPACTION_FAILED
        assert provider.call_count == 1

    async def test_provider_error_ends_the_run_with_a_reason(self, make_loop: Any) -> None:
        loop, _ = make_loop([[ProviderError("upstream exploded")]])
        events = await drain(loop, _user("hi"))

        warnings = [e for e in events if isinstance(e, Warning)]
        assert warnings[0].code == "provider_error"
        assert "upstream exploded" in warnings[0].message
        assert final_done(events).reason is ExitReason.PROVIDER_ERROR

    async def test_failure_mid_stream_still_answers_dispatched_tools(self, make_loop: Any) -> None:
        """A tool already dispatched when the stream dies must be accounted for.

        Otherwise the saved history has a tool_use with no tool_result and the
        next request is rejected.
        """
        loop, _ = make_loop(
            [
                [
                    StreamToolUse(id="toolu_1", name="read", arguments={"path": "hello.py"}),
                    ProviderError("connection reset"),
                ]
            ]
        )
        events = await drain(loop, _user("read it"))

        assert any(isinstance(e, ToolCallStarted) for e in events)
        assert final_done(events).reason is ExitReason.PROVIDER_ERROR

    async def test_stream_without_a_terminal_event_aborts_cleanly(self, make_loop: Any) -> None:
        loop, _ = make_loop([[StreamTextDelta("cut off with no StreamEnd")]])
        events = await drain(loop, _user("hi"))

        assert final_done(events).reason is ExitReason.ABORTED
