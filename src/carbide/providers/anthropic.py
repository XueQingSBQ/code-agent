"""Anthropic-compatible provider with a stable, provider-neutral stream."""

from __future__ import annotations

import asyncio
import json
import os
import random
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic

from carbide.providers.base import (
    APIError,
    InvalidRequestError,
    OverloadedError,
    PromptTooLongError,
    Provider,
    ProviderError,
    RateLimitError,
    StopReason,
    StreamEnd,
    StreamTextDelta,
    StreamThinkingDelta,
    StreamToolUse,
    Usage,
)

_KNOWN_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
}


class AnthropicProvider(Provider):
    """Anthropic Messages API provider.

    Vendor event parsing and retry policy live here.  The engine only sees
    provider-neutral stream records and never imports SDK event classes.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
        timeout: float = 600.0,
        base_url: str | None = None,
    ) -> None:
        env = _load_env()
        self.model = (
            model
            or os.environ.get("CARBIDE_MODEL")
            or env.get("CARBIDE_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or env.get("ANTHROPIC_MODEL")
            or "claude-sonnet-4-6"
        )
        self.max_retries = max(1, max_retries)
        self.timeout = timeout

        resolved_api_key = (
            api_key or os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
        )
        if not resolved_api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        resolved_base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_BASE_URL")
        )
        client_kwargs: dict[str, Any] = {
            "api_key": resolved_api_key,
            "timeout": timeout,
            "max_retries": 0,
        }
        if resolved_base_url:
            client_kwargs["base_url"] = resolved_base_url

        self._client = AsyncAnthropic(**client_kwargs)
        # ``client`` was part of the original public surface.
        self.client = self._client

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Yield a normalized stream and retry only before output is emitted."""

        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if system:
            request["system"] = self._prepare_system(system)
        if tools:
            request["tools"] = self._prepare_tools(tools)
        # Anthropic Messages API does not expose a temperature parameter in the
        # current SDK. Keep it in the provider protocol for other backends, but
        # never forward it here: doing so makes every request fail before it
        # reaches the network with ``unexpected keyword argument``.
        request.update(kwargs)
        request.pop("temperature", None)

        for attempt in range(self.max_retries):
            emitted = False
            state = self._new_stream_state()
            try:
                raw_stream = await self._client.messages.create(**request)
                async for raw_event in raw_stream:
                    for normalized in self._parse_event(raw_event, state):
                        emitted = True
                        yield normalized

                yield StreamEnd(
                    stop_reason=self._normalize_stop_reason(state["stop_reason"]),
                    usage=Usage(
                        input_tokens=state["input_tokens"],
                        output_tokens=state["output_tokens"],
                        cache_read_tokens=state["cache_read_tokens"],
                        cache_creation_tokens=state["cache_creation_tokens"],
                    ),
                    assistant_content=self._assembled_content(state),
                )
                return
            except Exception as exc:
                error = self._handle_error(exc)
                if error.retryable and not emitted and attempt < self.max_retries - 1:
                    delay = error.retry_after if isinstance(error, RateLimitError) else None
                    if delay is None:
                        delay = 0.5 * (2**attempt) + random.random() * 0.25
                    await asyncio.sleep(delay)
                    continue
                raise error from exc

    @staticmethod
    def _prepare_system(system: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """Add a safe prompt-cache breakpoint without mutating caller data."""

        if isinstance(system, str):
            return [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        blocks = [dict(block) for block in system]
        if blocks and not any(block.get("cache_control") for block in blocks):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    @staticmethod
    def _prepare_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cache the stable tool-schema prefix when no breakpoint was supplied."""

        prepared = [dict(tool) for tool in tools]
        if prepared and not any(tool.get("cache_control") for tool in prepared):
            prepared[-1]["cache_control"] = {"type": "ephemeral"}
        return prepared

    @staticmethod
    def _new_stream_state() -> dict[str, Any]:
        return {
            "blocks": {},
            "stop_reason": "end_turn",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

    def _parse_event(self, event: Any, state: dict[str, Any]) -> list[Any]:
        """Parse one SDK event, retaining enough state to assemble the turn."""

        event_type = getattr(event, "type", None)
        if event_type == "message_start":
            message = getattr(event, "message", None)
            usage = getattr(message, "usage", None)
            if usage is not None:
                state["input_tokens"] = self._usage_value(usage, "input_tokens")
                state["cache_read_tokens"] = self._usage_value(usage, "cache_read_input_tokens")
                state["cache_creation_tokens"] = self._usage_value(
                    usage, "cache_creation_input_tokens"
                )
            return []

        if event_type == "content_block_start":
            index = getattr(event, "index", 0)
            block = getattr(event, "content_block", None)
            block_type = getattr(block, "type", None)
            if block_type == "text":
                state["blocks"][index] = {"type": "text", "text": ""}
            elif block_type == "thinking":
                state["blocks"][index] = {
                    "type": "thinking",
                    "thinking": "",
                    "signature": "",
                }
            elif block_type == "redacted_thinking":
                state["blocks"][index] = {
                    "type": "redacted_thinking",
                    "data": getattr(block, "data", ""),
                }
            elif block_type == "tool_use":
                state["blocks"][index] = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input_json": "",
                    "input": {},
                }
            return []

        if event_type == "content_block_delta":
            index = getattr(event, "index", 0)
            delta = getattr(event, "delta", None)
            delta_type = getattr(delta, "type", None)
            block = state["blocks"].get(index)

            if delta_type == "text_delta":
                if block is None:
                    block = {"type": "text", "text": ""}
                    state["blocks"][index] = block
                text = getattr(delta, "text", "") or ""
                block["text"] = block.get("text", "") + text
                return [StreamTextDelta(text)]

            if delta_type == "thinking_delta":
                if block is None:
                    block = {"type": "thinking", "thinking": "", "signature": ""}
                    state["blocks"][index] = block
                text = getattr(delta, "thinking", "") or ""
                block["thinking"] = block.get("thinking", "") + text
                return [StreamThinkingDelta(text)]

            if delta_type == "signature_delta":
                if block is None:
                    block = {"type": "thinking", "thinking": "", "signature": ""}
                    state["blocks"][index] = block
                block["signature"] = block.get("signature", "") + (
                    getattr(delta, "signature", "") or ""
                )
                return []

            if delta_type == "input_json_delta":
                if block is None:
                    block = {
                        "type": "tool_use",
                        "id": "",
                        "name": "unknown",
                        "input_json": "",
                        "input": {},
                    }
                    state["blocks"][index] = block
                block["input_json"] = block.get("input_json", "") + (
                    getattr(delta, "partial_json", "") or ""
                )
            return []

        if event_type == "content_block_stop":
            index = getattr(event, "index", 0)
            block = state["blocks"].get(index)
            if not block or block.get("type") != "tool_use":
                return []
            raw_input = block.pop("input_json", "")
            try:
                parsed = json.loads(raw_input) if raw_input else {}
            except json.JSONDecodeError:
                parsed = {}
            block["input"] = parsed if isinstance(parsed, dict) else {}
            return [
                StreamToolUse(
                    id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    arguments=cast(dict[str, Any], block["input"]),
                )
            ]

        if event_type == "message_delta":
            delta = getattr(event, "delta", None)
            stop_reason = getattr(delta, "stop_reason", None)
            if stop_reason is not None:
                state["stop_reason"] = stop_reason
            usage = getattr(event, "usage", None)
            if usage is not None:
                # Official Anthropic streams put input/cache usage in
                # ``message_start`` and output usage here.  Several
                # Anthropic-compatible gateways return the complete usage
                # object in ``message_delta`` instead.  Merge both shapes;
                # assignment avoids double-counting if both events contain
                # the same fields.
                state["output_tokens"] = self._usage_value(usage, "output_tokens")
                for field, state_key in (
                    ("input_tokens", "input_tokens"),
                    ("cache_read_input_tokens", "cache_read_tokens"),
                    ("cache_creation_input_tokens", "cache_creation_tokens"),
                ):
                    value = self._usage_value(usage, field)
                    if value:
                        state[state_key] = value
            return []

        return []

    @staticmethod
    def _usage_value(usage: Any, field: str) -> int:
        """Read usage from SDK models and dict-shaped compatible gateways."""

        value = usage.get(field, 0) if isinstance(usage, Mapping) else getattr(usage, field, 0)
        return int(value or 0)

    @staticmethod
    def _normalize_stop_reason(value: Any) -> StopReason:
        if value in _KNOWN_STOP_REASONS:
            return cast(StopReason, value)
        return "end_turn"

    @staticmethod
    def _assembled_content(state: dict[str, Any]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for index in sorted(state["blocks"]):
            block = state["blocks"][index]
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                content.append({"type": "text", "text": block["text"]})
            elif block_type == "thinking" and block.get("thinking"):
                content.append(
                    {
                        "type": "thinking",
                        "thinking": block["thinking"],
                        "signature": block.get("signature", ""),
                    }
                )
            elif block_type == "redacted_thinking":
                content.append({"type": "redacted_thinking", "data": block.get("data", "")})
            elif block_type == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                )
        return content

    def _handle_error(self, error: Exception) -> ProviderError:
        if isinstance(error, ProviderError):
            return error

        message = str(error)
        lowered = message.lower()
        status_code = getattr(error, "status_code", None)
        response = getattr(error, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        if any(
            marker in lowered
            for marker in (
                "prompt is too long",
                "prompt_too_long",
                "context_length_exceeded",
                "context length",
                "too many tokens",
            )
        ):
            return PromptTooLongError(message)

        if isinstance(error, anthropic.RateLimitError) or status_code == 429 or "429" in lowered:
            retry_after: float | None = None
            headers = getattr(response, "headers", None)
            raw_retry_after = headers.get("retry-after") if headers is not None else None
            if raw_retry_after is not None:
                try:
                    retry_after = float(raw_retry_after)
                except (TypeError, ValueError):
                    retry_after = None
            result = RateLimitError(retry_after=retry_after)
            return result

        if status_code in {500, 502, 503, 504, 529} or any(
            marker in lowered for marker in ("bad gateway", "service unavailable", "overloaded")
        ):
            return OverloadedError(message, retryable=True)

        if isinstance(error, (anthropic.APIConnectionError, TimeoutError, OSError)):
            return ProviderError(message, retryable=True)

        if status_code is not None and status_code >= 400:
            if status_code == 400 or "invalid" in lowered:
                return InvalidRequestError(message)
            return APIError(message)

        if "invalid" in lowered or "bad request" in lowered:
            return InvalidRequestError(message)
        return APIError(message)

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        request: dict[str, Any] = {"model": self.model, "messages": messages}
        if system:
            request["system"] = system
        if tools:
            request["tools"] = tools
        try:
            result = await self._client.messages.count_tokens(**request)
            return int(result.input_tokens)
        except Exception:
            return sum(self.estimate_tokens(str(message)) for message in messages)

    def estimate_tokens(self, text: str) -> int:
        """Conservative local estimate used before an API call."""

        return max(1, (len(text) + 2) // 3)


def _load_env() -> dict[str, str]:
    """Load dotenv values without hiding the real process environment."""

    from dotenv import dotenv_values

    env_path = Path.cwd() / ".env"
    values = dotenv_values(env_path) if env_path.exists() else {}
    return {key: value for key, value in values.items() if value is not None}


def create_anthropic_provider(
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> AnthropicProvider:
    """Create an Anthropic-compatible provider from explicit or env values."""

    env = _load_env()
    return AnthropicProvider(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY"),
        model=model
        or os.environ.get("CARBIDE_MODEL")
        or env.get("CARBIDE_MODEL")
        or env.get("ANTHROPIC_MODEL"),
        base_url=base_url or os.environ.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_BASE_URL"),
    )
