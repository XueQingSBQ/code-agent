"""The Anthropic backend, driven by scripted raw stream events.

The fake client yields the SDK's own event models, not stand-ins. That is the
point: if a future SDK renames a field or changes an event shape, constructing
these objects fails and the test says so, instead of a mock happily agreeing
with code that no longer matches the wire protocol.

Nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from types import SimpleNamespace
from typing import Any

import anthropic
import anthropic.types as at
import httpx2 as httpx
import pytest
from anthropic.types.raw_message_delta_event import Delta as MessageStopDelta

from carbide.providers import anthropic as provider_module
from carbide.providers.anthropic import AnthropicProvider
from carbide.providers.base import (
    OverloadedError,
    PromptTooLongError,
    ProviderError,
    RateLimitError,
    StreamChunk,
    StreamEnd,
    StreamTextDelta,
    StreamThinkingDelta,
    StreamToolUse,
)

# ---------------------------------------------------------------------------
# raw event builders
# ---------------------------------------------------------------------------

Script = Sequence[Any]


def start(*, input_tokens: int = 100, cache_read: int = 0, cache_creation: int = 0) -> Any:
    return at.RawMessageStartEvent(
        type="message_start",
        message=at.Message(
            id="msg_1",
            type="message",
            role="assistant",
            model="claude-test",
            content=[],
            stop_reason=None,
            stop_sequence=None,
            usage=at.Usage(
                input_tokens=input_tokens,
                output_tokens=0,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
            ),
        ),
    )


def text_start(index: int) -> Any:
    return at.RawContentBlockStartEvent(
        type="content_block_start", index=index, content_block=at.TextBlock(type="text", text="")
    )


def text_delta(index: int, text: str) -> Any:
    return at.RawContentBlockDeltaEvent(
        type="content_block_delta", index=index, delta=at.TextDelta(type="text_delta", text=text)
    )


def thinking_start(index: int) -> Any:
    return at.RawContentBlockStartEvent(
        type="content_block_start",
        index=index,
        content_block=at.ThinkingBlock(type="thinking", thinking="", signature=""),
    )


def thinking_delta(index: int, text: str) -> Any:
    return at.RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=at.ThinkingDelta(type="thinking_delta", thinking=text),
    )


def signature_delta(index: int, signature: str) -> Any:
    return at.RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=at.SignatureDelta(type="signature_delta", signature=signature),
    )


def redacted_start(index: int, data: str) -> Any:
    return at.RawContentBlockStartEvent(
        type="content_block_start",
        index=index,
        content_block=at.RedactedThinkingBlock(type="redacted_thinking", data=data),
    )


def tool_start(index: int, *, call_id: str, name: str) -> Any:
    return at.RawContentBlockStartEvent(
        type="content_block_start",
        index=index,
        content_block=at.ToolUseBlock(type="tool_use", id=call_id, name=name, input={}),
    )


def json_delta(index: int, partial: str) -> Any:
    return at.RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=at.InputJSONDelta(type="input_json_delta", partial_json=partial),
    )


def block_stop(index: int) -> Any:
    return at.RawContentBlockStopEvent(type="content_block_stop", index=index)


def message_delta(stop_reason: Any, *, output_tokens: int = 25) -> Any:
    return at.RawMessageDeltaEvent(
        type="message_delta",
        delta=MessageStopDelta(stop_reason=stop_reason, stop_sequence=None),
        usage=at.MessageDeltaUsage(output_tokens=output_tokens),
    )


def unvalidated_message_delta(stop_reason: str) -> Any:
    """A stop reason no SDK version models, built past pydantic's validation.

    This is the shape of a *future* API addition. It cannot be constructed
    normally, which is the point: the guard exists for values that do not exist
    yet, so the test has to manufacture one.
    """
    return at.RawMessageDeltaEvent.model_construct(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason, stop_sequence=None),
        usage=at.MessageDeltaUsage(output_tokens=1),
    )


# ---------------------------------------------------------------------------
# fake client
# ---------------------------------------------------------------------------


def _response(status: int, *, headers: dict[str, str] | None = None) -> Any:
    return httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        headers=headers,
    )


def bad_request(message: str) -> anthropic.BadRequestError:
    return anthropic.BadRequestError(message, response=_response(400), body=None)


def rate_limited(retry_after: str | None = None) -> anthropic.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else None
    return anthropic.RateLimitError(
        "slow down", response=_response(429, headers=headers), body=None
    )


def status_error(code: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(f"http {code}", response=_response(code), body=None)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


class FakeMessages:
    """Stands in for ``client.messages``.

    Each entry in ``scripts`` is one call's worth of events. An exception in the
    list is raised when iteration reaches it, so a failure can be placed after
    some events have already been handed upstream — the case that matters for
    retry safety.
    """

    def __init__(self, scripts: Sequence[Script | BaseException]) -> None:
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []
        self.count_token_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append(kwargs)
        # An exhausted script means the provider retried more times than the test
        # accounted for. Returning an empty stream instead would quietly turn a
        # retry bug into a passing test.
        assert self._scripts, f"unscripted call #{len(self.calls)} to messages.create"
        script = self._scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        return _aiter(script)

    async def count_tokens(self, **kwargs: Any) -> Any:
        self.count_token_calls.append(kwargs)
        return SimpleNamespace(input_tokens=4242)


async def _aiter(events: Iterable[Any]) -> AsyncIterator[Any]:
    for event in events:
        if isinstance(event, BaseException):
            raise event
        yield event


def make_provider(
    scripts: Sequence[Script | BaseException], *, model: str = "claude-test"
) -> tuple[AnthropicProvider, FakeMessages]:
    provider = AnthropicProvider(api_key="test-key", model=model)
    messages = FakeMessages(scripts)
    # Reaching into the private client is deliberate: the alternative is a
    # constructor parameter that exists only for tests.
    provider._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return provider, messages


async def collect(provider: AnthropicProvider, **overrides: Any) -> list[StreamChunk]:
    request: dict[str, Any] = {
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "max_tokens": 1000,
    }
    request.update(overrides)
    return [chunk async for chunk in provider.stream(**request)]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of serving them.

    Swaps the module's ``asyncio`` reference rather than patching asyncio itself,
    which would slow every other coroutine in the test run.
    """
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(provider_module, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return slept


# ---------------------------------------------------------------------------
# assembling a turn
# ---------------------------------------------------------------------------


class TestStreamAssembly:
    async def test_text_and_tool_use_round_trip(self) -> None:
        """One ordinary turn: prose, then a tool call, then the terminal event."""
        provider, client = make_provider(
            [
                [
                    start(input_tokens=120, cache_read=900, cache_creation=30),
                    text_start(0),
                    text_delta(0, "Reading "),
                    text_delta(0, "the file."),
                    block_stop(0),
                    tool_start(1, call_id="toolu_a", name="read"),
                    json_delta(1, '{"path"'),
                    json_delta(1, ': "a.py"}'),
                    block_stop(1),
                    message_delta("tool_use", output_tokens=42),
                ]
            ]
        )
        chunks = await collect(provider)

        assert chunks[:2] == [StreamTextDelta("Reading "), StreamTextDelta("the file.")]
        assert chunks[2] == StreamToolUse(id="toolu_a", name="read", arguments={"path": "a.py"})

        end = chunks[3]
        assert isinstance(end, StreamEnd)
        assert end.stop_reason == "tool_use"
        assert end.usage.input_tokens == 120
        assert end.usage.output_tokens == 42
        assert end.usage.cache_read_tokens == 900
        assert end.usage.cache_creation_tokens == 30
        # The assistant message is assembled in block order, ready to be sent back.
        assert end.assistant_content == [
            {"type": "text", "text": "Reading the file."},
            {"type": "tool_use", "id": "toolu_a", "name": "read", "input": {"path": "a.py"}},
        ]
        # Streaming is requested explicitly; the SDK's accumulating helper is not
        # used, because it hides the per-block close events.
        assert client.calls[0]["stream"] is True
        assert client.calls[0]["model"] == "claude-test"

    async def test_tool_use_is_emitted_at_block_close_not_turn_end(self) -> None:
        """The dispatch win depends entirely on this ordering.

        The tool must be yielded while the model is still producing later blocks;
        if it only arrived with StreamEnd, the executor would have nothing to
        overlap with.
        """
        provider, _ = make_provider(
            [
                [
                    start(),
                    tool_start(0, call_id="toolu_x", name="glob"),
                    json_delta(0, '{"pattern": "**/*.py"}'),
                    block_stop(0),
                    text_start(1),
                    text_delta(1, "and now some prose"),
                    block_stop(1),
                    message_delta("tool_use"),
                ]
            ]
        )
        chunks = await collect(provider)
        kinds = [type(chunk).__name__ for chunk in chunks]
        assert kinds == ["StreamToolUse", "StreamTextDelta", "StreamEnd"]

    async def test_split_json_is_reassembled(self) -> None:
        """Arguments arrive as arbitrary string fragments, not whole JSON."""
        pieces = ['{"path": "s', 'rc/a.py", "li', 'mit": 10}']
        provider, _ = make_provider(
            [
                [
                    start(),
                    tool_start(0, call_id="t", name="read"),
                    *[json_delta(0, piece) for piece in pieces],
                    block_stop(0),
                    message_delta("tool_use"),
                ]
            ]
        )
        chunks = await collect(provider)
        assert chunks[0] == StreamToolUse(
            id="t", name="read", arguments={"path": "src/a.py", "limit": 10}
        )

    async def test_malformed_tool_json_becomes_empty_arguments(self) -> None:
        """A truncated block must not take the run down.

        Empty arguments produce a validation error the model can read and retry
        from, which is strictly more useful than a traceback.
        """
        provider, _ = make_provider(
            [
                [
                    start(),
                    tool_start(0, call_id="t", name="read"),
                    json_delta(0, '{"path": "a.py"'),  # never closed
                    block_stop(0),
                    message_delta("max_tokens"),
                ]
            ]
        )
        chunks = await collect(provider)
        assert chunks[0] == StreamToolUse(id="t", name="read", arguments={})

    async def test_tool_use_with_no_arguments_is_an_empty_dict(self) -> None:
        provider, _ = make_provider(
            [
                [
                    start(),
                    tool_start(0, call_id="t", name="ls"),
                    block_stop(0),
                    message_delta("tool_use"),
                ]
            ]
        )
        chunks = await collect(provider)
        assert chunks[0] == StreamToolUse(id="t", name="ls", arguments={})

    async def test_thinking_keeps_its_signature(self) -> None:
        """A thinking block without its signature is rejected on the next turn."""
        provider, _ = make_provider(
            [
                [
                    start(),
                    thinking_start(0),
                    thinking_delta(0, "let me check "),
                    thinking_delta(0, "the imports"),
                    signature_delta(0, "sig-part-1"),
                    signature_delta(0, "sig-part-2"),
                    block_stop(0),
                    text_start(1),
                    text_delta(1, "done"),
                    block_stop(1),
                    message_delta("end_turn"),
                ]
            ]
        )
        chunks = await collect(provider)
        assert chunks[0] == StreamThinkingDelta("let me check ")
        end = chunks[-1]
        assert isinstance(end, StreamEnd)
        assert end.assistant_content[0] == {
            "type": "thinking",
            "thinking": "let me check the imports",
            "signature": "sig-part-1sig-part-2",
        }

    async def test_redacted_thinking_is_passed_through_verbatim(self) -> None:
        """Opaque to us, but it still has to survive the round trip."""
        provider, _ = make_provider(
            [
                [
                    start(),
                    redacted_start(0, "EncryptedBlob=="),
                    block_stop(0),
                    message_delta("end_turn"),
                ]
            ]
        )
        chunks = await collect(provider)
        end = chunks[0]
        assert isinstance(end, StreamEnd)
        assert end.assistant_content == [{"type": "redacted_thinking", "data": "EncryptedBlob=="}]

    async def test_empty_text_blocks_are_dropped(self) -> None:
        """The API rejects a text block with an empty string.

        A turn that is only a tool call still opens a text block, so this is the
        common case, not an edge case.
        """
        provider, _ = make_provider(
            [
                [
                    start(),
                    text_start(0),
                    block_stop(0),
                    tool_start(1, call_id="t", name="read"),
                    json_delta(1, '{"path": "a.py"}'),
                    block_stop(1),
                    message_delta("tool_use"),
                ]
            ]
        )
        chunks = await collect(provider)
        end = chunks[-1]
        assert isinstance(end, StreamEnd)
        assert [block["type"] for block in end.assistant_content] == ["tool_use"]

    async def test_a_stream_with_no_message_delta_defaults_to_end_turn(self) -> None:
        provider, _ = make_provider([[start(), text_start(0), text_delta(0, "hi"), block_stop(0)]])
        chunks = await collect(provider)
        end = chunks[-1]
        assert isinstance(end, StreamEnd)
        assert end.stop_reason == "end_turn"

    async def test_unknown_stop_reason_does_not_leak_through(self) -> None:
        """SDK drift guard.

        A stop reason no build models must fall back to end_turn rather than
        becoming a StopReason value the engine will never match on. That is the
        safe default *only* because an unrecognised reason cannot be acted on;
        every reason we do understand has to be handled explicitly, which is what
        the next test checks.
        """
        provider, _ = make_provider(
            [
                [
                    start(),
                    text_start(0),
                    text_delta(0, "hi"),
                    block_stop(0),
                    unvalidated_message_delta("some_future_reason"),
                ]
            ]
        )
        chunks = await collect(provider)
        end = chunks[-1]
        assert isinstance(end, StreamEnd)
        assert end.stop_reason == "end_turn"

    async def test_every_stop_reason_the_sdk_models_is_modelled_here(self) -> None:
        """Drift in the other direction, and the dangerous one.

        A reason the SDK knows about but ``StopReason`` does not would be
        flattened to end_turn by the guard above — so an incomplete response
        would be reported as a finished one. That is how
        ``model_context_window_exceeded`` was found missing.
        """
        import typing

        from carbide.providers.base import StopReason

        # Message.stop_reason is Optional[Literal[...]]; the Literal is arg 0.
        sdk_reasons = set(
            typing.get_args(typing.get_args(at.Message.model_fields["stop_reason"].annotation)[0])
        )
        assert sdk_reasons <= set(typing.get_args(StopReason)), (
            "the SDK models a stop reason carbide does not: decide how the loop "
            "should react to it before widening the Literal"
        )

    async def test_window_exhausted_mid_response_is_reported_as_itself(self) -> None:
        """Not a truncation, and not a completion.

        The engine reacts to this by compacting, which it cannot do if the
        provider launders the reason into end_turn.
        """
        provider, _ = make_provider(
            [
                [
                    start(input_tokens=199_000),
                    text_start(0),
                    text_delta(0, "I was saying"),
                    message_delta("model_context_window_exceeded"),
                ]
            ]
        )
        chunks = await collect(provider)
        end = chunks[-1]
        assert isinstance(end, StreamEnd)
        assert end.stop_reason == "model_context_window_exceeded"

    async def test_deltas_without_a_preceding_start_are_tolerated(self) -> None:
        """Defensive: a dropped content_block_start must not raise KeyError."""
        provider, _ = make_provider(
            [[start(), text_delta(3, "orphaned"), message_delta("end_turn")]]
        )
        chunks = await collect(provider)
        assert chunks[0] == StreamTextDelta("orphaned")

    async def test_max_tokens_is_reported_so_the_loop_can_recover(self) -> None:
        provider, _ = make_provider(
            [[start(), text_start(0), text_delta(0, "half a sen"), message_delta("max_tokens")]]
        )
        chunks = await collect(provider)
        end = chunks[-1]
        assert isinstance(end, StreamEnd)
        assert end.stop_reason == "max_tokens"
        assert end.assistant_content == [{"type": "text", "text": "half a sen"}]


# ---------------------------------------------------------------------------
# error translation
# ---------------------------------------------------------------------------


class TestErrorTranslation:
    """The engine reacts to carbide's error types, never the vendor's.

    Mapping the wrong status to the wrong type is silently expensive: a
    PromptTooLongError misfiled as generic ends the run instead of compacting.
    """

    async def test_prompt_too_long_is_recognised(self) -> None:
        provider, _ = make_provider([bad_request("prompt is too long: 250000 tokens > 200000")])
        with pytest.raises(PromptTooLongError):
            await collect(provider)

    async def test_context_length_wording_also_matches(self) -> None:
        provider, _ = make_provider([bad_request("invalid_request_error: context_length_exceeded")])
        with pytest.raises(PromptTooLongError):
            await collect(provider)

    async def test_other_bad_requests_are_not_treated_as_too_long(self) -> None:
        """Compacting in response to a malformed request would hide the real bug."""
        provider, _ = make_provider([bad_request("tools.0.name: invalid value")])
        with pytest.raises(ProviderError) as caught:
            await collect(provider)
        assert not isinstance(caught.value, PromptTooLongError)
        assert not caught.value.retryable

    async def test_rate_limit_carries_retry_after(self) -> None:
        # Retryable, so every attempt has to be scripted; see TestRetryPolicy.
        provider, _ = make_provider([rate_limited("7")] * 3)
        with pytest.raises(RateLimitError) as caught:
            await collect(provider)
        assert caught.value.retry_after == 7.0
        assert caught.value.retryable

    async def test_unparsable_retry_after_falls_back_to_backoff(self) -> None:
        """The header may be an HTTP date, which float() cannot read."""
        provider, _ = make_provider([rate_limited("Wed, 21 Oct 2026 07:28:00 GMT")] * 3)
        with pytest.raises(RateLimitError) as caught:
            await collect(provider)
        assert caught.value.retry_after is None

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 529])
    async def test_server_errors_are_overload(self, code: int) -> None:
        provider, _ = make_provider([status_error(code)] * 3)
        with pytest.raises(OverloadedError) as caught:
            await collect(provider)
        assert caught.value.retryable

    @pytest.mark.parametrize("code", [401, 403, 404])
    async def test_client_errors_are_not_retryable(self, code: int) -> None:
        """Retrying a bad key three times just wastes the user's time."""
        provider, _ = make_provider([status_error(code)])
        with pytest.raises(ProviderError) as caught:
            await collect(provider)
        assert not caught.value.retryable

    async def test_connection_errors_are_retryable(self) -> None:
        """Includes timeouts. Nothing was committed, so a retry is safe."""
        provider, _ = make_provider([connection_error(), connection_error(), connection_error()])
        with pytest.raises(ProviderError) as caught:
            await collect(provider)
        assert caught.value.retryable


# ---------------------------------------------------------------------------
# retry policy
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    async def test_a_clean_failure_is_retried(self, no_real_sleep: list[float]) -> None:
        provider, client = make_provider(
            [
                status_error(529),
                [
                    start(),
                    text_start(0),
                    text_delta(0, "second try"),
                    block_stop(0),
                    message_delta("end_turn"),
                ],
            ]
        )
        chunks = await collect(provider)
        assert chunks[0] == StreamTextDelta("second try")
        assert len(client.calls) == 2
        assert len(no_real_sleep) == 1

    async def test_a_failure_after_output_is_not_retried(self, no_real_sleep: list[float]) -> None:
        """The bug this pins: retrying a partly consumed stream duplicates text.

        The caller has already rendered "the answer is " to the terminal. A
        replay would print it a second time, and the assembled assistant message
        would contain it twice.
        """
        provider, client = make_provider(
            [
                [
                    start(),
                    text_start(0),
                    text_delta(0, "the answer is "),
                    status_error(529),
                ],
                [
                    start(),
                    text_start(0),
                    text_delta(0, "the answer is 42"),
                    block_stop(0),
                    message_delta("end_turn"),
                ],
            ]
        )
        seen: list[StreamChunk] = []
        with pytest.raises(OverloadedError):
            async for chunk in provider.stream(
                system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=100
            ):
                seen.append(chunk)

        assert seen == [StreamTextDelta("the answer is ")]
        assert len(client.calls) == 1, "a consumed stream must not be replayed"
        assert no_real_sleep == []

    async def test_retries_are_bounded(self, no_real_sleep: list[float]) -> None:
        provider, client = make_provider([status_error(529)] * 5)
        with pytest.raises(OverloadedError):
            await collect(provider)
        assert len(client.calls) == 3
        assert len(no_real_sleep) == 2

    async def test_backoff_grows(self, no_real_sleep: list[float]) -> None:
        """Exponential with jitter: a fixed delay would synchronise clients."""
        provider, _ = make_provider([status_error(529)] * 3)
        with pytest.raises(OverloadedError):
            await collect(provider)
        first, second = no_real_sleep
        assert 0.5 <= first < 0.75
        assert 1.0 <= second < 1.25

    async def test_retry_after_overrides_backoff(self, no_real_sleep: list[float]) -> None:
        """The server knows better than the formula."""
        provider, _ = make_provider(
            [rate_limited("2"), [start(), text_start(0), text_delta(0, "ok"), block_stop(0)]]
        )
        await collect(provider)
        assert 2.0 <= no_real_sleep[0] < 2.25

    async def test_non_retryable_failures_fail_immediately(
        self, no_real_sleep: list[float]
    ) -> None:
        provider, client = make_provider([bad_request("prompt is too long"), []])
        with pytest.raises(PromptTooLongError):
            await collect(provider)
        assert len(client.calls) == 1
        assert no_real_sleep == []


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


async def test_the_sdk_does_not_retry_behind_our_back() -> None:
    """max_retries=0 is load-bearing.

    The SDK's own retry would replay a request whose stream we may already have
    partly consumed, which is exactly what this provider is careful not to do.
    """
    provider = AnthropicProvider(api_key="k", model="m")
    assert provider._client.max_retries == 0


async def test_base_url_is_forwarded_when_set() -> None:
    provider = AnthropicProvider(api_key="k", model="m", base_url="https://proxy.example/v1")
    assert str(provider._client.base_url).rstrip("/") == "https://proxy.example/v1"


async def test_count_tokens_passes_the_request_through() -> None:
    provider, client = make_provider([])
    total = await provider.count_tokens(
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "read"}],
    )
    assert total == 4242
    assert client.count_token_calls[0]["model"] == "claude-test"


def test_the_provider_satisfies_the_protocol() -> None:
    from carbide.providers.base import Provider

    assert isinstance(AnthropicProvider(api_key="k", model="m"), Provider)
