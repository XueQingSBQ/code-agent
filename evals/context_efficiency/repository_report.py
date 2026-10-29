"""Render the frozen real-repository experiment without selecting successes."""

# Chinese punctuation is intentional in the generated report.
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.context_efficiency.repository_ab import aggregate


def percent(value: float | None) -> str:
    return "无完整配对" if value is None else f"{value * 100:.1f}%"


def render(report: dict[str, Any], selection: dict[str, Any]) -> str:
    # Older dispatch processes may still write the earlier summary schema.
    # Derive presentation metrics from the preserved per-attempt records.
    report = {**report, "summary": aggregate(report["records"])}
    config = report["config"]
    composite = bool(selection.get("original_issue_ids"))
    description = (
        f"将 {len(selection['original_issue_ids'])} 个兼容的真实 GitHub 缺陷移植到同一 SymPy 提交，构成一个自主维护任务。"
        if composite
        else f"选取 {len(report['task_ids'])} 个 SWE-bench Lite 真实 GitHub 问题，来自 Flask 和 SymPy。"
    )
    selection_note = (
        "任务来自固定 manifest："
        + selection["manifest"].get("name", "unnamed")
        + "。"
        + selection.get("selection_policy", "")
        if selection.get("manifest")
        else "样本在模型执行前按数据集顺序筛选。"
    )
    lines = [
        "# Carbide 真实仓库上下文压缩评测",
        "",
        f"生成时间：{report['generated_at']}。模型："
        + ", ".join(sorted({r["model"] for r in report["records"]}))
        + "。",
        "",
        description,
        f"每个问题重复 {config['repetitions']} 次，已有 {report['completed_runs']}/{report['planned_runs']} 个运行槽位保存终态。"
        + (
            "评测尚未完成，以下结果为中期记录。"
            if report["completed_runs"] < report["planned_runs"]
            else ""
        ),
        "",
        "这是一项本地原生环境的 SWE-bench Lite 子集评测，不是官方 Docker Harness 的排行榜成绩。"
        "固定提交的源码通过 Git blob 哈希核验；每次运行使用独立进程、独立检出和真实 Carbide 工具。",
        "",
        "## 样本和独立评分",
        "",
        "模型只收到原始问题描述与环境说明，自主搜索、读取、修改、运行测试和维护 Todo。"
        "没有固定读文件顺序、重复日志填充、指定裁剪 ID 或模拟空闲时间。",
        "",
        selection_note
        + "原始代码必须复现至少一个失败，官方补丁必须通过全部指定的"
        "FAIL_TO_PASS 和 PASS_TO_PASS 测试，原始代码的 PASS_TO_PASS 必须通过。"
        "环境不适用的样本及原因保存在 selection.json；不会根据 Agent 是否修复成功更换样本。",
        "",
        "评分在新的检出中仅应用模型的生产 Python 代码补丁，再应用隐藏的原始回归测试补丁。"
        "模型对测试和测试配置的改动不进入评分。漏测、跳过、收集错误均不算通过。"
        "成功还要求正常完成且无运行时错误；超时和部分修复保留在分母。",
        "",
        "## 总体结果",
        "",
        "| 配置 | 完整任务通过 | 已报告输入 Token | 未缓存输入 | 缓存命中 | 平均 Agent 用时 | 触发压缩的运行 | 摘要请求 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    names = {"off": "关闭压缩", "production": "默认 200K", "pressure": "32K 压力条件"}
    for variant, metrics in report["summary"].items():
        lines.append(
            f"| {names[variant]} | {metrics['successful']}/{metrics['runs']} | {metrics['input_tokens']:,} | "
            f"{metrics['uncached_input']:,} | {percent(metrics['cache_hit_ratio'])} | "
            f"{metrics['mean_agent_seconds']:.1f}s | {metrics['compression_runs']} | {metrics['summary_requests']} |"
        )
    interruptions = report.get("interrupted_attempts", [])
    phases = report.get("execution_phases", [])
    lines += [
        "",
        "## 服务异常与计量质量",
        "",
        "| 配置 | Usage 有效完整 | 代码测试失败 | Provider 错误 | 全零或无效 Usage | 运行时失败 | 评分错误 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant, metrics in report["summary"].items():
        categories = metrics.get("failure_categories", {})
        lines.append(
            f"| {names[variant]} | {metrics['usage_complete_runs']}/{metrics['runs']} | "
            f"{categories.get('test_failure', 0)} | {categories.get('provider_error', 0)} | "
            f"{categories.get('invalid_provider_usage', 0)} | "
            f"{categories.get('runtime_failure', 0) + categories.get('infrastructure_failure', 0)} | "
            f"{categories.get('grading_error', 0)} |"
        )
    lines += [
        "",
        f"另有 {len(interruptions)} 次评测器中断记录，保存在 interrupted/，不覆盖原补丁和请求。"
        f"中断尝试额外记录了 {sum(r['usage']['requests'] for r in interruptions):,} 个请求、"
        f"{sum(r['usage']['prompt_tokens'] for r in interruptions):,} 个已报告输入 Token；"
        "这些消耗不混入后续新尝试的配对效果。逻辑运行槽位与实际发起的尝试分别记录。",
        "",
        "非空请求的总输入为零视为计量无效；未缓存输入为零但缓存读取大于零仍合法。"
        "服务错误与计量异常保留在端到端失败率中，不能直接解释为模型修复能力失败。"
        "表中输入是已报告 Usage 的总和，存在缺失计量时不能当作完整消耗。",
        "",
        "恢复仅允许改变调度并发，原始冻结配置和运行时代码保持不变。"
        "首次批量运行的并发保存在 experiment.json；新增执行阶段记录的实际并发："
        + (", ".join(str(p["parallel"]) for p in phases) if phases else str(config["parallel"]))
        + "。遇到账号耗尽或无效 Usage 停止派发新尝试，已启动工作收尾后保存记录。",
    ]
    if composite:
        lines += [
            "",
            "组合工作负载来源: " + ", ".join(selection["original_issue_ids"]) + "。"
            "问题移植后的官方修复组合通过独立回归测试，随后才固定工作负载。"
            "它是一个派生任务，不算四个独立样本，也不与原始提交上的单问题结果混算。",
            "",
        ]
    lines += [
        "",
        "## 配对比较",
        "",
        "| 配置 | Usage 完整的配对 | 全尝试加权输入减少 | 各任务平均输入减少 | 任务 bootstrap 95% 区间 | 双方均成功的配对 | 双方均成功时输入减少 |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for variant, metrics in report["summary"].items():
        if variant == "off":
            continue
        ci = metrics["task_bootstrap_95_ci"]
        lines.append(
            f"| {names[variant]} | {metrics['complete_pairs']} | {percent(metrics['weighted_input_reduction'])} | "
            f"{percent(metrics['mean_task_input_reduction'])} | "
            f"{percent(ci[0]) + ' 至 ' + percent(ci[1]) if ci else '样本不足'} | "
            f"{metrics['both_successful_pairs']} | {percent(metrics['both_successful_input_reduction'])} |"
        )
    lines += [
        "",
        "## 实际压缩触发与额外开销",
        "",
        "| 配置 | 峰值单请求输入 | 压缩事件 | 触发层 | 摘要输入 Token | 净释放估算 Token | 有效配对中触发压缩 | 配对未缓存输入减少 |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for variant, metrics in report["summary"].items():
        lines.append(
            f"| {names[variant]} | {metrics.get('peak_prompt_tokens', 0):,} | "
            f"{metrics.get('compression_events', 0)} | "
            f"{', '.join(metrics['layer_counts']) or '无'} | "
            f"{metrics.get('summary_input_tokens', 0):,} | "
            f"{metrics.get('estimated_net_tokens_freed', 0):,} | "
            f"{metrics.get('triggered_complete_pairs', 0)} | "
            f"{percent(metrics.get('paired_uncached_input_reduction'))} |"
        )
    lines += [
        "",
        "净释放是压缩器的本地估算，不是累计 Provider 输入减少。摘要输入和文件恢复会消耗上下文；"
        "多次摘要、缓存失效可能使未缓存输入增加。只有实际触发压缩的运行能够观察压缩机制，"
        "仍需结合完整任务通过率判断是否有效。",
    ]
    lines += [
        "",
        "所有摘要请求均计入 Provider Usage。缓存输入也属于总输入；未验证实际中转单价，"
        "不把 Token 减少换算为费用减少。Usage 不完整的尝试保留在成功率中，但不会作为零输入制造节省。",
        "",
        "全尝试指标可能受提前失败影响，必须和成功率一起看；双方均成功的指标仅为补充。"
        "置信区间按问题聚类：先对同一问题的重复结果求均值，再重采样问题，不能把模型请求当独立样本。",
        "",
        "## 逐任务结果",
        "",
        "| 真实问题 | 关闭压缩通过 | 默认 200K 通过 | 32K 压力条件通过 | 实际触发层 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in report["records"]:
        grouped[record["instance_id"]].append(record)
    for task_id in report["task_ids"]:
        runs = grouped[task_id]
        columns = []
        for variant in ("off", "production", "pressure"):
            selected = [r for r in runs if r["variant"] == variant]
            columns.append(
                "not_run" if not selected else f"{sum(r['success'] for r in selected)}/{len(selected)}"
            )
        layers = sorted({layer for run in runs for layer in run["layers"]})
        task = next(item for item in selection["selected"] if item["instance_id"] == task_id)
        issue = task_id.rsplit("-", 1)[1]
        label = (
            task_id
            if composite
            else f"[{task_id}](https://github.com/{task['repo']}/issues/{issue})"
        )
        lines.append(f"| {label} | " + " | ".join(columns) + f" | {', '.join(layers) or '无'} |")
    lines += ["", "## 失败样本", ""]
    failures = [record for record in report["records"] if not record["success"]]
    if not failures:
        lines.append("当前样本没有失败；该结果仅适用于所选子集，不能推出生产成功率为 100%。")
    for record in failures:
        validation = record["validation"]
        failed_tests = {
            name: status
            for key in ("fail_to_pass", "pass_to_pass")
            for name, status in validation.get(key, {}).items()
            if status != "passed"
        }
        lines.append(
            f"- {record['instance_id']}/{record['variant']}/repeat={record['repeat']}："
            f"类别 {record.get('failure_category', '未分类')}；结束原因 {record['exit_reason']}；运行错误 {record['errors']}；"
            f"评分错误 {validation.get('error', '无')}；未通过的指定测试 {failed_tests}。"
        )
    lines += [
        "",
        "## 解释边界",
        "",
        "- 默认预算与压力条件分别报告；32K 的结果不能写成默认 200K 的生产效果。",
        "- 短任务没有触发压缩时，差异主要来自模型执行路径和缓存，不能归因于压缩。",
        "- 这些是真实缺陷修复任务，但仅覆盖两个 Python 仓库，不覆盖前端、部署、持续多需求开发或多 Agent。",
        "- 没有强制触发 L2/L3；如未触发，只能说明本次工作负载未覆盖，不等于验证其生产效果。",
        "- 时间包含真实工具执行与 API 等待；并发负载和中转缓存影响耗时，不能给出生产 SLA。",
        "- 历史合成实验的 41.1% 和 88.7% 不与这次真实任务混算。",
        "",
        "## 复现与原始记录",
        "",
        "```powershell",
        *(
            [
                "uv run python -m evals.context_efficiency.repository_session `",
                "  --source eval_results/context-efficiency/repository-ab `",
                f"  --output eval_results/context-efficiency/repository-session --issues {len(selection['original_issue_ids'])}",
            ]
            if composite
            else [
                "uv run python -m evals.context_efficiency.repository_ab `",
                "  --output eval_results/context-efficiency/repository-ab `",
                f"  --cases {config['cases']} --repetitions {config['repetitions']} --parallel {config['parallel']} `",
                f"  --variants {' '.join(config['variants'])} --max-turns {config['max_turns']} --timeout-seconds {config['timeout_seconds']}",
            ]
        ),
        "```",
        "",
        "输出包含冻结配置和运行时代码指纹、原始数据集、源码哈希、依赖清单、预检查、"
        "每次模型请求的完整上下文及 Usage、工具事件、提交补丁、独立测试 XML 和失败日志。"
        "同目录重跑只读取已完成记录，不把失败偷偷重试成成功。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.results / "report.json").read_text(encoding="utf-8"))
    selection = json.loads((args.results / "selection.json").read_text(encoding="utf-8"))
    args.output.write_text(render(report, selection), encoding="utf-8")


if __name__ == "__main__":
    main()
