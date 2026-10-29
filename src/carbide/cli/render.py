"""Terminal rendering.

Inline rendering with Rich, deliberately not a full-screen TUI. A framework that
owns the alternate screen buffer breaks native terminal selection and throws away
scrollback, which is the wrong trade for a tool whose output people copy out of
and scroll back through.
"""

from __future__ import annotations

import contextlib

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from carbide.events import (
    CompactionOccurred,
    Done,
    Event,
    ExitReason,
    PermissionRequested,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    Warning,
)

_TOOL_STYLE = "bold cyan"
_WARN_STYLE = "yellow"
_ERR_STYLE = "bold red"
_DIM = "dim"


def _first_line(text: str, limit: int = 120) -> str:
    """First line of a tool preview, safe on empty content.

    A tool that fails with an empty message is rare but reachable — an OSError
    with no args, for one. Indexing splitlines()[0] would then crash the CLI on
    the error path, which is the worst possible place to crash.
    """
    line = text.strip().splitlines()[0] if text.strip() else "(no output)"
    return line[:limit]


class Renderer:
    """Turns the engine's event stream into terminal output."""

    def __init__(self, console: Console | None = None, *, verbose: bool = False) -> None:
        # Force UTF-8 encoding on Windows to handle Unicode properly
        import sys

        if sys.platform == "win32":
            # Set console encoding to UTF-8
            reconfigure = getattr(sys.stdout, "reconfigure", None)
            if callable(reconfigure):
                with contextlib.suppress(AttributeError):
                    reconfigure(encoding="utf-8")

        self.console = console or Console(legacy_windows=False, force_terminal=True)
        self.verbose = verbose
        self._buffer: list[str] = []
        self._usage = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        }

    def reset_usage(self) -> None:
        """Start a fresh query without carrying telemetry across CLI requests."""

        self._buffer.clear()
        self._usage = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        }

    def handle(self, event: Event) -> None:
        match event:
            case TextDelta(text):
                self._buffer.append(text)
                self.console.print(text, end="", markup=False, highlight=False)

            case ThinkingDelta(text) if self.verbose:
                self.console.print(Text(text, style=_DIM), end="", markup=False)

            case ToolCallStarted(name=name, arguments=arguments):
                self._flush()
                summary = self._summarise(name, arguments)
                self.console.print(
                    Text.assemble(("→ ", _TOOL_STYLE), (name, _TOOL_STYLE), (f" {summary}", _DIM))
                )

            case ToolCallFinished(name=name, is_error=is_error, duration_ms=ms, preview=preview):
                if is_error:
                    # Name the tool: with parallel dispatch, several are in flight
                    # and a bare error line does not say which one failed.
                    self.console.print(
                        Text.assemble(
                            ("  X ", _ERR_STYLE),
                            (name, _ERR_STYLE),
                            (f" {_first_line(preview)}", _DIM),
                        )
                    )
                elif self.verbose:
                    self.console.print(Text(f"  OK {name} ({ms:.0f}ms)", style=_DIM))

            case PermissionRequested(name=name, reason=reason):
                # M0 has no gate, so nothing blocks here yet. Rendering it now
                # means the event is not silently invisible when M2 starts
                # emitting it — an unhandled event in this match is a no-op.
                self._flush()
                self.console.print(
                    Text.assemble(("? ", _WARN_STYLE), (f"{name} needs approval: {reason}", _DIM))
                )

            case CompactionOccurred() as event_:
                self.console.print(
                    Text(
                        f"  <> {event_.layer}: freed {event_.tokens_freed:,} tokens"
                        + (f" ({event_.detail})" if event_.detail else ""),
                        style=_DIM,
                    )
                )

            case TurnCompleted() as turn:
                self._usage["input"] += turn.input_tokens
                self._usage["output"] += turn.output_tokens
                self._usage["cache_read"] += turn.cache_read_tokens
                self._usage["cache_creation"] += turn.cache_creation_tokens
                if self.verbose:
                    self.console.print(
                        Text(
                            f"  turn {turn.turn}: in {turn.input_tokens:,} "
                            f"(cached {turn.cache_read_tokens:,}) out {turn.output_tokens:,}",
                            style=_DIM,
                        )
                    )

            case Warning(code=code, message=message):
                self.console.print(Text.assemble(("! ", _WARN_STYLE), (f"{code}: {message}", _DIM)))

            case Done() as done:
                self._flush()
                self._print_footer(done)

    def _flush(self) -> None:
        """Re-render buffered prose as markdown once it is complete."""
        if not self._buffer:
            return
        raw = "".join(self._buffer)
        self._buffer.clear()
        # The streamed text is already on screen as plain text; only re-render
        # when it actually contains markdown worth formatting.
        if any(marker in raw for marker in ("```", "\n- ", "\n#", "**")):
            self.console.print()
            self.console.print(Markdown(raw))
        else:
            self.console.print()

    def _print_footer(self, done: Done) -> None:
        style = _DIM if done.reason is ExitReason.COMPLETED else _WARN_STYLE
        cached = self._usage["cache_read"]
        created = self._usage["cache_creation"]
        # Anthropic reports these three disjoint input buckets: uncached,
        # cache-read and cache-created.  The old denominator omitted the
        # cache-created bucket, which overstated the hit rate on a cold cache.
        total_prompt = self._usage["input"] + cached + created
        hit_rate = (cached / total_prompt * 100) if total_prompt else 0.0
        self.console.print(
            Text(
                f"[{done.reason}] {done.turns} turn(s) · "
                f"in {total_prompt:,} "
                f"(uncached {self._usage['input']:,}, read {cached:,}, write {created:,}) "
                f"out {self._usage['output']:,} · hit {hit_rate:.0f}%",
                style=style,
            )
        )

    @staticmethod
    def _summarise(name: str, arguments: dict[str, object]) -> str:
        """One-line argument summary; never dump full file contents."""
        for key in ("path", "pattern", "command"):
            if key in arguments:
                value = str(arguments[key]).replace("\n", " ")
                return value[:100] + ("…" if len(value) > 100 else "")
        return ""
