"""Test CLI with cache tracking."""

import asyncio
import sys
from pathlib import Path

from carbide.cli.app import run_repl
from carbide.providers.anthropic import AnthropicProvider

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')


async def main():
    provider = AnthropicProvider(
        api_key="pwd",
        model="gpt-5.6-luna",
        base_url="http://127.0.0.1:8080",
    )

    await run_repl(
        provider=provider,
        project_dir=Path.cwd(),
        enable_compression=False,
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
