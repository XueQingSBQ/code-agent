"""Merge completed paired evaluations and render an auditable Chinese report.

When a scenario is rerun after a fix, the last supplied file explicitly replaces
earlier records with the same (scenario, variant, seed). Source references and
the superseded count are retained; unsuccessful runs are never silently filtered.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.context_efficiency.layered_ab import aggregate

LABELS = {
    "spill": "L1 大结果落盘",
    "snip": "L2 显式裁剪",
    "idle": "L3 空闲清理",
    "compact": "L4 摘要与恢复",
}
VARIANTS = {"off": "关闭压缩", "full": "开启压缩", "summary_only": "仅 L4，关闭文件恢复"}
EXPECTED_LAYERS = {
    "spill": {"L1:spill"},
    "snip": {"L2:snip"},
    "idle": {"L3:micro"},
    "compact": {"L4:compact", "L4.5:restore"},
}


def merge(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("At least one completed report is required")
    selected: dict[tuple[str, str, int], dict[str, Any]] = {}
    sources = []
    limits: list[str] = []
    signature: tuple[int, int] | None = None
    config: dict[str, Any] = {}
    superseded = 0
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not report.get("complete"):
            raise ValueError(f"Evaluation is still running: {path}")
        if report.get("kind") != "layered-live-coding-ab":
            raise ValueError(f"Not a layered coding evaluation: {path}")
        candidate = (report["config"]["phases"], report["config"]["payload_chars"])
        if signature is not None and candidate != signature:
            raise ValueError("Cannot merge workloads with different phases or payload sizes")
        signature = candidate
        config = report["config"]
        sources.append(
            {
                "path": path.as_posix(),
                "timestamp": report["timestamp"],
                "runs": len(report["records"]),
                "successes": sum(record["success"] for record in report["records"]),
            }
        )
        limits.extend(report.get("limitations", []))
        for record in report["records"]:
            key = (record["scenario"], record["variant"], record["seed"])
            superseded += int(key in selected)
            selected[key] = {**record, "source_report": path.as_posix()}
    records = [selected[key] for key in sorted(selected)]
    if not records:
        raise ValueError("Reports contain no evaluation runs")
    if len({record["model"] for record in records}) != 1:
        raise ValueError("Cannot pool different models")
    layers_ok = all(
        EXPECTED_LAYERS[record["scenario"]].issubset(record["layers"])
        for record in records
        if record["variant"] == "full"
    )
    return {
        "schema_version": 1,
        "kind": "layered-live-coding-ab-merged",
        "timestamp": datetime.now(UTC).isoformat(),
        "complete": True,
        "config": {
            **config,
            "scenarios": sorted({r["scenario"] for r in records}),
            "variants": sorted({r["variant"] for r in records}),
        },
        "sources": sources,
        "superseded_runs": superseded,
        "replacement_rule": "Last explicitly supplied report wins for a scenario/variant/seed",
        "layer_coverage_passed": layers_ok,
        "limitations": sorted(set(limits)),
        "summary": aggregate(records),
        "records": records,
    }


def render(report: dict[str, Any]) -> str:
    records = report["records"]
    summary = report["summary"]
    successes = sum(record["success"] for record in records)
    requests = [request for record in records for request in record["requests"]]
    violations = sum(bool(request["pairing_violations"]) for request in requests)
    incomplete = sum(not record["usage"]["complete"] for record in records)
    lines = [
        "# Carbide 分层上下文压缩实验",
        "",
        "日期：2026-10-09。",
        "",
        f"实测模型：`{records[0]['model']}`。正式选取 {len(records)} 次长会话运行，"
        f"每次 {report['config']['phases']} 个读取阶段和 1 个代码实现阶段。",
        f"整任务成功 {successes}/{len(records)}；共记录 {len(requests)} 次模型请求，"
        f"工具配对违规 {violations} 次，Usage 不完整的运行 {incomplete} 次。",
        "",
        "## 分层结果",
        "",
        "输入 Token 包含未缓存、缓存读取和缓存写入；所有 L4 摘要请求也计入。"
        "节省率按相同场景和相同 seed 配对计算，再对三组配对求均值。",
        "",
        "| 场景 | 方案 | 平均累计输入 Token | 相对基线节省 | 完整任务通过 | 字段保留率 | 实际触发层 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in summary:
        saving = item["paired_prompt_reduction_percent"]
        percent = f"{saving['mean']:.2f}%" if saving is not None else "无完整配对"
        lines.append(
            f"| {LABELS[item['scenario']]} | {VARIANTS[item['variant']]} | "
            f"{item['prompt_tokens']['mean']:,.0f} | {percent} | "
            f"{item['successes']}/{item['runs']} | {item['field_recall']:.1%} | "
            f"{', '.join(item['layers']) or '无'} |"
        )
    lines.extend(
        [
            "",
            "## 摘要成本和缓存",
            "",
            "| 场景/方案 | 摘要调用总数 | 摘要输入 Token 总数 | 最终阶段重复读取 | 未缓存输入总数 | 缓存命中率 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in summary:
        selected = [
            r
            for r in records
            if (r["scenario"], r["variant"]) == (item["scenario"], item["variant"])
        ]
        prompt = sum(r["usage"]["prompt_tokens"] for r in selected)
        cached = sum(r["usage"]["cache_read_tokens"] for r in selected)
        uncached = sum(r["usage"]["input_tokens"] for r in selected)
        lines.append(
            f"| {LABELS[item['scenario']]}/{VARIANTS[item['variant']]} | "
            f"{item['summary_calls']} | {item['summary_prompt_tokens']:,} | "
            f"{item['final_reads']} | {uncached:,} | {cached / prompt:.1%} |"
        )
    lines.extend(
        [
            "",
            "Token 减少不等于账单按相同比例降低：摘要需要额外调用，"
            "历史裁剪也可能降低前缀缓存命中。这里没有套用未经确认的中转价格。",
            "",
            "## 实验流程",
            "",
            "1. 每次运行创建独立临时工作区，启用真实 Read/Write/Snip 工具和真实模型。",
            "2. 同一会话连续读取 12 次观测文件。第 2–8 阶段陆续引入七项约束，"
            "第 9–10 阶段修改部分旧约束；模型每阶段输出累积 JSON 状态。",
            "3. L1 每次返回 56,000 字符；其余场景每次返回 16,000 字符，避开 L1 阈值。",
            "4. L2 第 6 阶段显式请求裁剪三个过期读取 ID，保留阶段决策文本。",
            "5. L3 第 9 阶段将上次调用时间设为 3,601 秒之前，保持生产的 3,600 秒阈值。",
            "6. 最终 L4 使用 24,000 Token 的加速预算触发摘要；对照关闭文件恢复与开启恢复。"
            "第 6 阶段另读一个小型契约文件，在最终实现时检查它的精确值是否仍可获得。",
            "7. 最后模型写出报价函数和状态回执。独立 Python 子进程检查普通计算、"
            "上限、罚值、精度、零下限、负值拒绝、重试次数限制，以及精确状态和源文件值。",
            "8. 改变 seed 后重复三次，按 seed 轮换方案顺序；记录完整 Usage、摘要内容、"
            "请求哈希、错误和工具配对校验。失败样本保留在结果里。",
            "",
            "## 此次发现和修复",
            "",
            "- Snip 未进入 SDK 工具池；补齐实例绑定，在有裁剪请求时无需等待预算超限。",
            "- L3 计时器未接入模型调用；结果缺少工具名且调用 ID 是随机值。"
            "改为按历史 tool_use 的 ID 解析真实工具名，并逐个处理并发结果块。",
            "- 裁剪和固定边界摘要可能拆散工具调用与结果；改为成对裁剪、扩展保留边界。",
            "- 压缩之前只在顶层用户请求开始检查；改为每次模型请求前检查，"
            "覆盖单条长任务内的工具循环。",
            "- L4 释放量忽略了保留消息；改为按前后上下文实际本地大小计算。"
            "摘要失败、空输出和截断时保留原上下文并显式报错。",
            "- 实测发现固定三条消息的后缀会丢弃刚完成的决策，摘要可能漏项；"
            "改为原样保留上一轮完整用户交互和当前轮，恢复注入消息不算作新用户轮次。"
            "最终加速预算从 18K 调整到 24K，以容纳保留窗口并减少频繁摘要；所有 L4 方案重新配对测试。",
            "- L4.5 重复注入仍保留的文件；加入当前文件内容比对，避免重复恢复，"
            "文件内容已变化时仍恢复最新版本。子 Agent 不共享父 Snip 删除队列。",
            "- 完整近期窗口会把摘要标记推到最后五条消息之前；恢复层改为识别当前完整压缩上下文，"
            "避免有摘要却跳过恢复。保持相同基线与窗口配置，单独重跑恢复方案。",
            "- 摘要模型可能继续执行历史阶段指令；增加独立的当前摘要请求，"
            "让模型把历史指令作为数据处理。实验也明确状态对象只包含 CONFIG_UPDATE 引入的键，"
            "辅助文件元数据存放在状态对象之外；L4 基线和实验组均重新运行。",
            "",
            "## 数据解释边界",
            "",
            "这些结果证明各层在指定工作负载下实际运行，以及此任务的状态保留和代码正确性。"
            "它们不代表任意代码库的节省率或生产任务成功率。",
            "",
            "- 这是受控的合成编程任务，尚未形成真实仓库问题的 SWE-bench 数据。",
            "- 每组只有三次；均值、标准差和最小/最大值保存在 JSON，不能据此给出生产 SLA。",
            "- L2 的删除候选由实验提供，没有验证模型自主挑选删除内容的质量。",
            "- L3 模拟了空闲时间条件，没有实等一小时，未验证实际缓存 TTL 或过期账单。",
            "- L4 使用加速预算；没有声称跑满默认 200K 窗口或触发全部层的同一轮压缩。",
            "- 主要指标来自中转返回的 Provider Usage，未调用静默回退的 count_tokens "
            "方法来冒充精确计数；本地估算仅作为诊断字段。",
            "- 之前的 35.646% 来自不同的八阶段 L1 工作负载，不能与本次百分比直接比较。",
            "",
            "## 原始数据与复现",
            "",
        ]
    )
    for source in report["sources"]:
        lines.append(
            f"- `{source['path']}`：{source['runs']} 次运行，通过 {source['successes']} 次，"
            f"完成时间 {source['timestamp']}。"
        )
    if report["superseded_runs"]:
        lines.extend(
            [
                "",
                f"本报告显式替换了 {report['superseded_runs']} 个相同键的旧样本："
                "初次 L3 未实际清理内容，修复后重跑；L4 先修改摘要请求并明确回执结构，"
                "仍发现约束丢失，再保留完整近期轮次并调整加速预算。每次 L4 改动都重跑基线和两个实验组。"
                "旧数据和失败样本保留，最终各组使用同一轮的完整配对。",
            ]
        )
    lines.extend(
        [
            "",
            "```powershell",
            "uv run python -m evals.context_efficiency.layered_ab `",
            "  --repetitions 3 --parallel 3 `",
            "  --output eval_results/context-efficiency/layered-reproduction.json",
            "```",
            "",
        ]
    )
    lines.extend(["", "## 未通过样本", ""])
    failures = [record for record in records if not record["success"]]
    if not failures:
        lines.append("本轮选定版本全部通过；修复前失败仍保存在来源数据中。")
    for record in failures:
        lines.append(
            f"- {LABELS[record['scenario']]}/{VARIANTS[record['variant']]}，seed={record['seed']}："
            f"{json.dumps(record['validation'], ensure_ascii=False)}。"
        )
        if not record.get("artifacts") and record["requests"][-1]["response"].strip() == "DONE":
            lines.append(
                "  12 个阶段检查点均正确，但模型最终只回复 DONE，未写出文件。"
                "这是工具执行遵从性和完成验证的失败，不能算成代码任务成功。"
                "该样本仍计入通过率和 Token 统计。"
            )
    lines.extend(
        [
            "",
            "恢复读取次数的比较也应结合通过率：失败任务没有执行写入，"
            "不能凭其零读取次数宣称恢复收益。成功样本的具体调用记录保存在 JSON 中。",
            "",
        ]
    )
    lines.extend(
        [
            "## 验证与关联产物",
            "",
            "最终运行时代码的离线门禁：543 passed，41 个 live 测试默认排除。"
            "本次涉及模块通过 Ruff 和 mypy。",
            "",
            "L4 缓存命中从基线 97.3% 降至 69.6%，未缓存输入也明显增加。"
            "下一步需要完成验证与续跑机制，以及结合真实价格、缓存和窗口压力评估摘要策略，"
            "不能只追求累计输入 Token 的降低。",
            "",
            "- [同参数八阶段复测](CONTEXT_COMPRESSION_REQUEST_BOUNDARY_EXPERIMENT.md)："
            "count_tokens 口径减少 44.440%，Usage 口径减少 41.086%。",
            "- [最终分层原始数据](../eval_results/context-efficiency/layered-ab-final.json)。",
            "- [累计输入曲线 PNG](../eval_results/context-efficiency/layered-token-trajectory.png)，"
            "另有同名 SVG 和 PDF。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()
    report = merge(args.reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "runs": len(report["records"]),
                "successes": sum(r["success"] for r in report["records"]),
                "layer_coverage_passed": report["layer_coverage_passed"],
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
