# Context Compression Experiment

Date: 2026-09-19

## Result

The controlled live experiment measured L1 spill compression against the same
Anthropic-compatible Provider endpoint and model configured in `.env`.

| Metric | Compression on | Compression off |
| --- | ---: | ---: |
| Paired runs | 5 | 5 |
| Provider `count_tokens` before request | 45,508 | 45,508 |
| Provider `count_tokens` after request | 2,089 | 45,508 |
| Completion rate | 100% (5/5) | 100% (5/5) |
| Compression layers | L1:spill | none |
| Mean output tokens | 17.2 | 18.6 |
| Mean streamed request duration | 2,098.6 ms | 2,464.8 ms |

The primary result is:

```text
(45,508 - 2,089) / 45,508 = 95.410% token reduction
```

The local compression event independently reported 43,416 tokens freed per
run, which is consistent with the Provider count difference of 43,419 tokens.

The raw report is [controlled-ab-final.json](/E:/claude/eval_results/context-efficiency/controlled-ab-final.json).

## Method

The experiment ran five paired repetitions. Every pair used the same valid
conversation history:

- three assistant `tool_use` / user `tool_result` sequences;
- one 60,000-character tool result per sequence;
- the same final prompt: `CONTROLLED_COMPRESSION_OK`;
- the same compression budget: 50,000 tokens;
- the same Provider endpoint and model.

The compression-on Agent ran the normal QueryLoop and allowed L1 spill to
replace each large tool result with a bounded preview and a `.carbide/spill`
reference. The compression-off Agent sent the original history unchanged.

For each side, the runner records both:

1. Provider `count_tokens` before and after the request. This is the primary
   context-size measurement.
2. Streaming usage fields (`input_tokens`, `cache_read_tokens`, and
   `cache_creation_tokens`). These are retained for cost/cache diagnostics,
   but are not used as the primary reduction metric because the endpoint
   reports cached input separately.

Reproduce it with:

```bash
uv run --env-file .env python -m evals.context_efficiency.controlled_ab \
  --repetitions 5 \
  --segments 3 \
  --payload-chars 60000 \
  --compression-budget 50000 \
  --output eval_results/context-efficiency/controlled-ab-final.json
```

The runner is [controlled_ab.py](/E:/claude/evals/context_efficiency/controlled_ab.py:1).

## What this proves

This is strong evidence that L1 spill reduces the actual request context for
large tool results, without reducing completion on this controlled final
request. It also validates that the Provider's exact token-counting endpoint
agrees with Carbide's local `tokens_freed` accounting for this case.

## What this does not prove

This is not yet a general five-layer task benchmark:

- L2 snip, L3 micro-compaction, L4 summarization, and L4.5 restoration were
  not isolated in this run.
- The history is synthetic and intentionally dominated by large tool results.
- The final task is a short response check, not a multi-file coding task.
- Five repetitions are enough for a controlled regression signal, not for a
  production SLA or a general model-quality claim.
- The result does not establish that every workload receives a 95.41%
  reduction.

The corresponding long-session experiment is documented in
[CONTEXT_COMPRESSION_LONG_TASK_EXPERIMENT.md](/E:/claude/docs/CONTEXT_COMPRESSION_LONG_TASK_EXPERIMENT.md).
The remaining production evaluation should use a fixed suite of real coding
tasks and run an ablation matrix for L1, L1-L2, L1-L3, L1-L4, and L1-L4.5 while
measuring task completion, patch correctness, tests, exact Provider token
counts, cache usage, cost, and latency.
