"""Replay real maintenance requests as successive user turns in one Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from evals.context_efficiency.repository_ab import (
    Config,
    instance_from_record,
    run,
    task_prompt,
)
from evals.context_efficiency.repository_environment import save_json


def prepare(source: Path, combined: Path, output: Path) -> dict[str, Any]:
    """Reuse the prechecked compatible combination; never expose reference fixes."""
    original = json.loads((source / "selection.json").read_text(encoding="utf-8"))
    compatibility = json.loads((combined / "selection.json").read_text(encoding="utf-8"))
    source_ids = compatibility["original_issue_ids"]
    checks = {item["instance_id"]: item for item in compatibility["checks"]}
    if not source_ids or any(
        not checks[issue]["eligible"] or not checks[issue]["reference"]["success"]
        for issue in source_ids
    ):
        raise ValueError("Every combined reference prefix must pass its independent precheck")
    originals = {item["instance_id"]: item for item in original["selected"]}
    task = dict(compatibility["selected"][0])
    task["instance_id"] = f"sympy-followup-{len(source_ids)}-issues"
    python = Path(task["python"])
    prompts = []
    for index, issue in enumerate(source_ids):
        prompt = task_prompt(instance_from_record(originals[issue]), python)
        if index:
            prompt = (
                "Continue in this same checkout and conversation. The previous repairs must "
                "remain working. An additional maintenance request has arrived:\n\n" + prompt
            )
        prompts.append(prompt)
    task["followup_prompts"] = prompts
    result = {
        "requested": 1,
        "kind": "one real maintenance conversation with sequential issue requests",
        "original_issue_ids": source_ids,
        "checks": compatibility["checks"],
        "selected": [task],
    }
    for key in ("manifest", "manifest_path", "selection_policy"):
        if key in compatibility:
            result[key] = compatibility[key]
    selection_path = output / "selection.json"
    if selection_path.exists():
        saved: dict[str, Any] = json.loads(selection_path.read_text(encoding="utf-8"))
        if saved != result:
            raise ValueError("Refusing to change a previously frozen follow-up workload")
        return saved
    save_json(selection_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--combined", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--parallel", type=int, default=1)
    args = parser.parse_args()
    prepare(args.source.resolve(), args.combined.resolve(), args.output.resolve())
    if not args.prepare_only:
        asyncio.run(
            run(
                args.output.resolve(),
                Config(cases=1, repetitions=3, parallel=args.parallel, max_turns=60, timeout_seconds=1800),
            )
        )


if __name__ == "__main__":
    main()
