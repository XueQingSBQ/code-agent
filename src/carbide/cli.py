"""CLI entry point for Carbide agent."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from carbide.sdk import create_agent


async def main() -> None:
    """Simple CLI for testing."""

    print("Carbide Agent (Simple Test)")
    print("=" * 50)

    # Create agent
    agent = create_agent(
        project_dir=Path.cwd(),
        enable_memory=False,  # Start simple
        enable_skills=False,
        enable_rules=False,
        enable_compression=False,
    )

    print("Agent initialized")
    print(f"Project: {Path.cwd()}")
    print()

    # Simple REPL
    while True:
        try:
            # Get user input
            user_input = (await asyncio.to_thread(input, "You: ")).strip()

            if not user_input:
                continue

            if user_input.lower() in ["exit", "quit", "q"]:
                break

            # Query agent
            print("Assistant: ", end="", flush=True)

            async for event in agent.query(user_input):
                if event.type == "chunk" and event.data.content:
                    print(event.data.content, end="", flush=True)
                elif event.type == "error":
                    print(f"\nError: {event.data}", file=sys.stderr)

            print("\n")

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except EOFError:
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

    # Show stats
    print("\nSession stats:")
    stats = agent.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")


def run() -> None:
    """Entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
