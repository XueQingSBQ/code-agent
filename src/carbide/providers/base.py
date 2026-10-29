"""Provider interface and Anthropic implementation.

Provider abstracts the LLM API, supporting:
- Streaming responses
- Token counting
- Prompt caching
- Error handling and retries
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TokenUsage:
    """Token usage statistics."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.input_tokens + self.output_tokens


@dataclass
class StreamToolUse:
    """A completed tool use block from streaming."""

    id: str  # Tool use ID
    name: str  # Tool name
    arguments: dict[str, Any]  # Parsed JSON arguments


@dataclass(frozen=True)
class Usage:
    """Provider-neutral token usage for a completed streamed turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class StreamTextDelta:
    """Provider-neutral text delta."""

    text: str


@dataclass(frozen=True)
class StreamThinkingDelta:
    """Provider-neutral extended-thinking delta."""

    text: str


StopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
]


@dataclass(frozen=True)
class StreamEnd:
    """Terminal event containing the assembled assistant content."""

    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    assistant_content: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamChunk:
    """A chunk from streaming response."""

    type: str  # "content_block_start" | "content_block_delta" | "content_block_stop" | "message_delta" | "message_stop"
    content: str | None = None  # Text content
    tool_use_id: str | None = None  # For tool_use blocks
    tool_name: str | None = None
    tool_input: str | None = None  # Partial JSON
    usage: TokenUsage | None = None  # Token usage (in message_delta events)


@dataclass
class Message:
    """A complete message."""

    role: str  # "user" | "assistant"
    content: list[dict[str, Any]]  # Content blocks
    stop_reason: str | None = None
    usage: TokenUsage | None = None


class Provider(ABC):
    """
    Abstract LLM provider.

    Implementations must handle:
    - Streaming with proper chunk parsing
    - Token counting (approximate or exact)
    - Prompt caching (if supported)
    - Retries and rate limiting
    """

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """
        Stream a completion.

        Args:
            messages: Conversation history
            system: System prompt (string or structured)
            tools: Available tools
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Provider-specific options

        Yields:
            StreamChunk: Chunks of the response
        """
        raise NotImplementedError

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> Message:
        """Collect a stream into one provider-neutral message.

        Context compaction needs a bounded one-shot summary call.  Keeping the
        default implementation here means every provider only has to implement
        the streaming seam while still exposing a consistent completion API.
        """

        text_parts: list[str] = []
        assistant_content: list[dict[str, Any]] = []
        stop_reason: str | None = None
        usage: TokenUsage | None = None
        async for chunk in self.stream(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        ):
            if isinstance(chunk, StreamTextDelta):
                text_parts.append(chunk.text)
            elif isinstance(chunk, StreamEnd):
                assistant_content = chunk.assistant_content
                stop_reason = chunk.stop_reason
                usage = TokenUsage(
                    input_tokens=chunk.usage.input_tokens,
                    output_tokens=chunk.usage.output_tokens,
                    cache_creation_input_tokens=chunk.usage.cache_creation_tokens,
                    cache_read_input_tokens=chunk.usage.cache_read_tokens,
                )
            elif isinstance(chunk, StreamChunk) and chunk.content:
                text_parts.append(chunk.content)

        if text_parts and not assistant_content:
            assistant_content = [{"type": "text", "text": "".join(text_parts)}]
        return Message(
            role="assistant",
            content=assistant_content,
            stop_reason=stop_reason,
            usage=usage,
        )

    @abstractmethod
    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """
        Count tokens for a request.

        Args:
            messages: Messages to count
            system: System prompt
            tools: Tools to count

        Returns:
            Approximate or exact token count
        """
        pass

    @abstractmethod
    def estimate_tokens(self, text: str) -> int:
        """
        Fast local token estimation.

        Used for hot-path budget checks.
        Should be conservative (overestimate).

        Args:
            text: Text to estimate

        Returns:
            Estimated token count
        """
        pass


class ProviderError(Exception):
    """Base exception for provider errors."""

    def __init__(self, message: str = "provider error", *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class PromptTooLongError(ProviderError):
    """The upstream rejected a request because the prompt is too large."""


class OverloadedError(ProviderError):
    """The upstream is overloaded and the request may be retried."""


class RateLimitError(ProviderError):
    """Rate limit exceeded."""

    def __init__(self, retry_after: float | None = None):
        super().__init__("Rate limit exceeded", retryable=True)
        self.retry_after = retry_after


class ContextLengthError(ProviderError):
    """Context length exceeded."""

    pass


class InvalidRequestError(ProviderError):
    """Invalid request (e.g., bad parameters)."""

    pass


class APIError(ProviderError):
    """Generic API error."""

    pass
