"""Persist a small live endpoint probe without exposing credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carbide.providers.anthropic import AnthropicProvider
from evals.context_efficiency.repository_environment import save_json


async def probe(request_path: Path | None = None) -> dict[str, Any]:
    provider = AnthropicProvider(max_retries=1, timeout=25)
    result: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "model": provider.model}
    request = (
        json.loads(await asyncio.to_thread(request_path.read_text, encoding="utf-8"))
        if request_path
        else {
            "messages": [{"role": "user", "content": "Reply with exactly READY."}],
            "max_tokens": 64,
        }
    )
    try:
        async with asyncio.timeout(30):
            response = await provider.complete(**request)
        result["usage"] = asdict(response.usage) if response.usage is not None else None
        result["stop_reason"] = response.stop_reason
        text = "".join(block.get("text", "") for block in response.content)
        result["text"] = text
        result["request_snapshot"] = str(request_path) if request_path else None
        result["available"] = (
            (bool(response.content) if request_path else text.strip() == "READY")
            and response.usage is not None
            and (
                response.usage.input_tokens
                + response.usage.cache_read_input_tokens
                + response.usage.cache_creation_input_tokens
                > 0
            )
        )
    except Exception as exc:
        result.update(available=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        await provider.client.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    result = asyncio.run(probe(args.request))
    save_json(args.output, result)
    print(result)


if __name__ == "__main__":
    main()
