"""Plot long-session cumulative input from the final JSON (requires matplotlib)."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

LABELS = {
    "spill": "L1: large result spill",
    "snip": "L2: explicit pruning",
    "idle": "L3: simulated idle cleanup",
    "compact": "L4 + file restoration",
}
VARIANTS = {
    "off": ("Compression off", "#64748b"),
    "full": ("Compression on", "#2563eb"),
    "summary_only": ("L4 without file restoration", "#d97706"),
}


def cumulative(record: dict[str, Any]) -> list[int]:
    values = []
    running = 0
    for phase in record["phases"]:
        running += phase["usage"]["prompt_tokens"]
        values.append(running)
    values.append(record["usage"]["prompt_tokens"])
    return values


def plot(report: dict[str, Any], output: Path) -> None:
    figure, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
    for axis, scenario in zip(axes.flat, LABELS, strict=True):
        for variant, (label, color) in VARIANTS.items():
            selected = [
                record
                for record in report["records"]
                if (record["scenario"], record["variant"]) == (scenario, variant)
            ]
            if not selected:
                continue
            columns = list(zip(*(cumulative(record) for record in selected), strict=True))
            means = [statistics.fmean(column) for column in columns]
            deviations = [statistics.stdev(column) if len(column) > 1 else 0 for column in columns]
            stages = list(range(1, len(columns) + 1))
            axis.plot(stages, means, color=color, label=f"{label} (n={len(selected)})", linewidth=2)
            axis.fill_between(
                stages,
                [m - d for m, d in zip(means, deviations, strict=True)],
                [m + d for m, d in zip(means, deviations, strict=True)],
                color=color,
                alpha=0.12,
            )
        enabled = [
            r for r in report["records"] if r["scenario"] == scenario and r["variant"] == "full"
        ]
        title = f"{LABELS[scenario]} ({sum(r['success'] for r in enabled)}/{len(enabled)} passed)"
        axis.set_title(title, loc="left", fontweight="bold", fontsize=12)
        axis.set_xlabel("Stage (13 = final code implementation)")
        axis.set_ylabel("Cumulative provider input tokens")
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1_000_000:.2f}M"))
        axis.grid(alpha=0.2)
        axis.legend(fontsize=8, loc="upper left")
        axis.spines[["top", "right"]].set_visible(False)
    figure.suptitle("Carbide: long-session context compression", fontsize=17, fontweight="bold")
    figure.supxlabel(
        "Synthetic tasks; failed trials retained. Input includes cache + summaries; bands = sample SD.",
        fontsize=9,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg", "pdf"):
        figure.savefig(output.with_suffix(f".{extension}"), dpi=180)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plot(json.loads(args.input.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    main()
