"""Manual CLI test to verify cache display."""

import asyncio
import sys
from pathlib import Path

from rich.console import Console

from carbide.cli.render import Renderer
from carbide.events import Done, ExitReason, TextDelta, TurnCompleted
from carbide.providers.anthropic import AnthropicProvider
from carbide.sdk import Agent

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')


async def main():
    console = Console()
    renderer = Renderer(console, verbose=False)

    provider = AnthropicProvider(
        api_key="pwd",
        model="gpt-5.6-luna",
        base_url="http://127.0.0.1:8080",
    )

    agent = Agent(
        provider=provider,
        project_dir=Path.cwd(),
        enable_compression=False,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )

    # Query 1
    console.print("\n[bold cyan]Query 1: 你好[/bold cyan]")
    turn_count = 0
    async for event in agent.query("你好"):
        if event.type == "chunk" and event.data:
            if event.data.type == "content_block_delta" and event.data.content:
                renderer.handle(TextDelta(text=event.data.content))
        elif event.type == "turn_complete":
            turn_count += 1
            if isinstance(event.data, dict):
                turn_completed = TurnCompleted(
                    turn=event.data.get("turn", turn_count),
                    input_tokens=event.data.get("input_tokens", 0),
                    output_tokens=event.data.get("output_tokens", 0),
                    cache_read_tokens=event.data.get("cache_read_tokens", 0),
                    cache_creation_tokens=event.data.get("cache_creation_tokens", 0),
                )
                renderer.handle(turn_completed)
        elif event.type == "error":
            renderer.handle(Done(reason=ExitReason.PROVIDER_ERROR, turns=turn_count or 1))
            break
    else:
        renderer.handle(Done(reason=ExitReason.COMPLETED, turns=turn_count or 1))

    # Query 2
    console.print("\n[bold cyan]Query 2: 再见[/bold cyan]")
    turn_count = 0
    async for event in agent.query("再见"):
        if event.type == "chunk" and event.data:
            if event.data.type == "content_block_delta" and event.data.content:
                renderer.handle(TextDelta(text=event.data.content))
        elif event.type == "turn_complete":
            turn_count += 1
            if isinstance(event.data, dict):
                turn_completed = TurnCompleted(
                    turn=event.data.get("turn", turn_count),
                    input_tokens=event.data.get("input_tokens", 0),
                    output_tokens=event.data.get("output_tokens", 0),
                    cache_read_tokens=event.data.get("cache_read_tokens", 0),
                    cache_creation_tokens=event.data.get("cache_creation_tokens", 0),
                )
                renderer.handle(turn_completed)
        elif event.type == "error":
            renderer.handle(Done(reason=ExitReason.PROVIDER_ERROR, turns=turn_count or 1))
            break
    else:
        renderer.handle(Done(reason=ExitReason.COMPLETED, turns=turn_count or 1))


if __name__ == "__main__":
    asyncio.run(main())
