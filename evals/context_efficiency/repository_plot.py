"""Plot real-repository outcomes and paired measurements from saved records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# matplotlib is supplied by the desktop evaluation runtime rather than the
# project virtualenv used for the agent itself.
import matplotlib  # type: ignore[import-not-found]

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # type: ignore[import-not-found]
from matplotlib.ticker import FuncFormatter  # type: ignore[import-not-found]

PROFILES = ("off", "production", "pressure")
LABELS = ("Compression off", "Default 200K", "Pressure 32K")
COLORS = ("#64748b", "#2563eb", "#d97706")


def plot(report: dict[str, Any], output: Path) -> None:
    """Keep failures, missing usage and untriggered compression visible."""
    figure, axes = plt.subplots(2, 2, figsize=(12, 8.5), constrained_layout=True)
    summary = report["summary"]
    profiles = [name for name in PROFILES if name in summary]
    labels = [LABELS[PROFILES.index(name)] for name in profiles]
    colors = [COLORS[PROFILES.index(name)] for name in profiles]

    axis = axes[0, 0]
    passed = [summary[name]["successful"] for name in profiles]
    failed = [summary[name]["runs"] - value for name, value in zip(profiles, passed, strict=True)]
    axis.bar(labels, passed, label="Passed independent tests", color="#16a34a")
    axis.bar(
        labels, failed, bottom=passed, label="Failed / service or runtime error", color="#e2e8f0"
    )
    for index, name in enumerate(profiles):
        item = summary[name]
        axis.text(
            index,
            item["runs"] + 0.4,
            f"{item['successful']}/{item['runs']} passed",
            ha="center",
            fontsize=9,
        )
    axis.set_title("Task outcomes: all completed attempts", loc="left", fontweight="bold")
    axis.set_ylabel("Attempts")
    axis.margins(y=0.25)
    axis.legend(fontsize=8, loc="upper left")

    axis = axes[0, 1]
    for index, name in enumerate(profiles):
        records = [r for r in report["records"] if r["variant"] == name]
        for record in records:
            tokens = record["usage"]["prompt_tokens"]
            marker = "o" if record["usage"]["complete"] else "x"
            axis.scatter(index, tokens, color=colors[index], marker=marker, alpha=0.5, s=22)
        reported = [r["usage"]["prompt_tokens"] for r in records]
        if reported:
            axis.scatter(
                index, sum(reported) / len(reported), color="black", marker="_", s=260, linewidths=2
            )
    axis.set_xticks(range(len(profiles)), labels)
    axis.set_title("Reported total input per attempt", loc="left", fontweight="bold")
    axis.set_ylabel("Input tokens (cache + summaries included)")
    axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1_000_000:.1f}M"))
    axis.text(
        0.02,
        0.98,
        "Circle: complete usage; cross: incomplete\nBlack line: mean reported input",
        transform=axis.transAxes,
        va="top",
        fontsize=8,
    )

    axis = axes[1, 0]
    experimental = [name for name in profiles if name != "off"]
    pair_labels = []
    for index, name in enumerate(experimental):
        item = summary[name]
        mean = item["mean_task_input_reduction"]
        interval = item["task_bootstrap_95_ci"]
        if mean is not None:
            axis.scatter(mean * 100, index, color=COLORS[PROFILES.index(name)], s=50)
            if interval is not None:
                axis.plot(
                    [interval[0] * 100, interval[1] * 100],
                    [index, index],
                    color=COLORS[PROFILES.index(name)],
                    linewidth=2,
                )
        pair_labels.append(
            f"{LABELS[PROFILES.index(name)]}: {item['complete_pairs']} complete pairs; "
            f"{item['triggered_complete_pairs']} triggered compression"
        )
    axis.text(0.02, 0.02, "\n".join(pair_labels), transform=axis.transAxes, fontsize=8)
    axis.axvline(0, color="#94a3b8", linewidth=1)
    axis.set_yticks(
        range(len(experimental)), [LABELS[PROFILES.index(name)] for name in experimental]
    )
    axis.set_ylim(-0.75, max(1.75, len(experimental) - 0.25))
    axis.set_title("Paired input reduction: mean by issue", loc="left", fontweight="bold")
    axis.set_xlabel("Reduction (%) / negative = more input")
    axis.text(
        0.02,
        0.98,
        "95% bootstrap intervals cluster by issue\nFailures retained; early exits affect input",
        transform=axis.transAxes,
        va="top",
        fontsize=8,
    )

    axis = axes[1, 1]
    cache = [summary[name]["cache_hit_ratio"] * 100 for name in profiles]
    axis.bar(labels, cache, color=colors)
    for index, name in enumerate(profiles):
        item = summary[name]
        axis.text(index, cache[index] + 2, f"{cache[index]:.1f}%", ha="center", fontsize=9)
        axis.text(
            index,
            7,
            f"{item['compression_runs']} compressed\n{item['summary_requests']} summaries",
            ha="center",
            fontsize=8,
            color="white",
        )
    axis.set_ylim(0, 112)
    axis.set_ylabel("Cache-read input / reported total input (%)")
    axis.set_title("Cache use and compression activity", loc="left", fontweight="bold")

    for axis in axes.flat:
        axis.grid(axis="y", alpha=0.18)
        axis.set_axisbelow(True)
        axis.spines[["top", "right"]].set_visible(False)
        axis.tick_params(labelsize=9)
    completed, planned = report["completed_runs"], report["planned_runs"]
    state = "complete" if completed == planned else "partial"
    models = ", ".join(sorted({record["model"] for record in report["records"]}))
    title = (
        "Combined real-issue maintenance"
        if report.get("original_issue_ids")
        else "Real-repository coding evaluation"
    )
    figure.suptitle(
        f"Carbide: {title}\n{completed}/{planned} attempts ({state}); {models}",
        fontsize=15,
        fontweight="bold",
    )
    figure.supxlabel(
        "Native SWE-bench-derived evaluation. No billing claim. Default and pressure settings are separate.",
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
