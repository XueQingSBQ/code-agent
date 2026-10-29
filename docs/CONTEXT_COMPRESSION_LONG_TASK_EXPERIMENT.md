# Context Compression Long-Task Experiment

Date: 2026-09-19

Follow-up (2026-10-09): the layer-specific live coding evaluation, including
L2/L3/L4 and post-summary file restoration, is documented in
[CONTEXT_COMPRESSION_LAYERED_EXPERIMENT.md](CONTEXT_COMPRESSION_LAYERED_EXPERIMENT.md).
The 35.646% result below remains the historical L1 workload result, rather than
a measurement of the whole compression pyramid.

## Result

This experiment keeps one Agent session alive for eight sequential task phases.
Each phase makes two real model requests: one request to call a deterministic
`observe_phase` tool and one request to produce the phase result. The tool
returns 56,000 characters of observation data, so the conversation grows over
time instead of starting with an already oversized synthetic history.

Two paired repetitions were run with the same Provider, model, prompts, tool
payloads, and phase order.

| Metric | Compression on | Compression off |
| --- | ---: | ---: |
| Paired long-task runs | 2 | 2 |
| Phases per run | 8 | 8 |
| Model requests per run | 16 | 16 |
| Exact Provider request tokens, mean per run | 583,976 | 907,448 |
| Peak single request, mean per run | 84,919 | 113,159 |
| Full task completion | 2/2 | 2/2 |
| Phase completion | 100% | 100% |
| Compression layers | L1:spill | none |
| Mean wall-clock duration | 46,680.1 ms | 51,780.3 ms |

The primary long-task result is:

```text
(907,448 - 583,976) / 907,448 = 35.646% cumulative request-token reduction
```

The peak single-request reduction is:

```text
(113,159 - 84,919) / 113,159 = 24.956%
```

The compression-enabled run triggered L1 spill once per repetition during
phase 7 and reported 80,862 locally freed tokens per event. All eight phases,
including the final exact `LONG_TASK_OK` verification, completed successfully
in both variants.

## Method

The runner is [long_task_ab.py](/E:/claude/evals/context_efficiency/long_task_ab.py).
It creates a fresh temporary workspace for each repetition and uses the live
Provider configured through `.env`. Memory, skills, rules, governance, session
persistence, subagents, and Todo tools are disabled so they cannot change the
workload between A/B variants.

Each phase follows this protocol:

1. The user asks the model to process the next phase.
2. The model must call `observe_phase` exactly once.
3. The tool returns the deterministic 56,000-character observation.
4. The model must return the exact phase completion marker.
5. The next phase starts in the same Agent session.

The experiment records the exact `count_tokens` result for every captured model
request, including both the tool-selection request and the post-tool response
request. It also records stream usage, cache-read tokens, cache-creation
tokens, tool-call counts, compression events, phase correctness, and duration.

Reproduce it with:

```bash
uv run --env-file .env python -m evals.context_efficiency.long_task_ab \
  --repetitions 2 \
  --phases 8 \
  --payload-chars 56000 \
  --compression-budget 100000 \
  --output eval_results/context-efficiency/long-task-ab.json
```

Raw data: [long-task-ab.json](/E:/claude/eval_results/context-efficiency/long-task-ab.json).

## Interpretation

This is the more relevant measurement for a long-running Agent: the first
several phases have the same cost, then compression reduces the cost of later
requests while preserving task progress. The cumulative saving is therefore
lower than the 95.410% result from the isolated L1 payload experiment.

The stream usage summary also shows lower total prompt accounting with
compression (609,822 versus 908,996), but this is an auxiliary metric because
the gateway reports normal input and cached input in separate fields. The exact
Provider `count_tokens` sum remains the primary comparison.

## Limits

This experiment provides evidence for a long multi-turn L1 workload, but it is
not yet a general coding-task benchmark:

- The workload uses a deterministic observation tool rather than a real code
  change and test workflow.
- Only L1 spill fired. L2 snip, L3 micro-compaction, L4 summarization, and
  L4.5 restoration were not isolated here.
- Two repetitions are enough to expose the long-session trajectory, not enough
  to establish a production latency or cost SLA.
- The result does not imply that every long task will save 35.646%.

The next production-grade evaluation should use a fixed suite of real coding
tasks, repeat each task with and without compression, and report completion,
patch correctness, test results, exact request tokens, cache usage, cost, and
latency per compression ablation level.
