"""Run a fresh, bounded real-issue evaluation using verified cached environments.

No task is chosen based on its previous model result. The complete frozen input
selection is reused; grading happens independently in the repository harness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.context_efficiency.repository_ab import Config, run
from evals.context_efficiency.repository_environment import save_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--timeout-seconds", type=int, default=420)
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        parser.error("Use a new output directory; existing experiments are never overwritten")
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    for item in selection["selected"]:
        for key in ("cache", "python"):
            if not Path(item[key]).exists():
                parser.error(f"Missing cached {key} for {item['instance_id']}")
    config = Config(
        cases=len(selection["selected"]),
        repetitions=1,
        parallel=args.parallel,
        variants=("off", "production"),
        max_turns=args.max_turns,
        timeout_seconds=args.timeout_seconds,
    )
    save_json(root / "selection.json", selection)
    asyncio.run(run(root, config))


if __name__ == "__main__":
    main()
