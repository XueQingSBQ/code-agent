# Context efficiency evaluations

The repository evaluations use real GitHub issues from SWE-bench Lite, with
autonomous repository exploration, real Carbide tools and independent test
grading. They complement the earlier controlled and synthetic layer experiments;
their results must not be pooled.

## Original-commit repository subset

```powershell
uv run python -m evals.context_efficiency.repository_ab `
  --output eval_results/context-efficiency/repository-ab `
  --cases 20 --repetitions 3 --parallel 2 `
  --variants off production pressure --max-turns 60 --timeout-seconds 600
```

Preparation selects environment-valid cases in dataset order before the model
runs. Original production code must fail at least one designated FAIL_TO_PASS
test; designated PASS_TO_PASS tests must pass. The reference patch must pass all
designated tests. Source archives use fixed commits with Python source/test Git
blob hash verification. Local native Windows environments differ from the
official Docker harness: this is **not an official SWE-bench leaderboard score**.

Each attempt has a fresh process and checkout. The model sees the original issue
and environment guidance, without reference patches, hidden tests, prescribed
tool order or padding. All variants expose the same tool schema. Memory, skills,
rules and subagents are disabled. Model changes to tests and test configuration
do not enter the fresh grading checkout. Missing, skipped and collection-error
tests never pass. A successful run also requires clean Agent completion.

The variants are: compression off, the default 200K budget, and an explicitly
labelled 32K pressure budget. Variant order rotates between repetitions of each
task. Repeated attempts are clustered by task for bootstrap intervals.

## Fixed representative task set

The 20-case historical run is retained for audit purposes, but it is not the
recommended dataset for a resume or a new claim. Its selection followed dataset
order and mixed very different regression-test sizes. The reproducible small
set is defined in `evals/context_efficiency/real_task_manifest.json`:

```powershell
uv run python -m evals.context_efficiency.repository_ab `
  --output eval_results/context-efficiency/repository-real-v1 `
  --manifest evals/context_efficiency/real_task_manifest.json `
  --prepare-only
```

The manifest contains six real SWE-bench Lite issues: two Flask tasks and four
SymPy tasks covering configuration/API compatibility, CLI behavior, matrix code
generation, symbolic semantics, core API semantics and matrix indexing. The
manifest order and metadata are frozen; an unknown or duplicate issue is a hard
preflight error. Each issue must pass all of these checks before model runs:

- the original checkout fails at least one designated FAIL_TO_PASS test;
- every designated PASS_TO_PASS test passes on the original checkout;
- the gold patch passes the complete selected test-file set;
- the native Windows environment has no collection error or timeout.

Run one complete 12-attempt pilot batch before spending repetitions:

```powershell
uv run python -m evals.context_efficiency.repository_ab `
  --output eval_results/context-efficiency/repository-real-v1 `
  --manifest evals/context_efficiency/real_task_manifest.json `
  --cases 6 --repetitions 1 --parallel 1 `
  --variants off pressure --max-turns 30 --timeout-seconds 900
```

Only after all 12 slots have terminal records should the set be repeated three
times. Provider errors, invalid usage, timeouts and test failures are separate
categories; an unrun slot is `not_run` and is never represented as `0/0`.

## Combined maintenance workload

```powershell
uv run python -m evals.context_efficiency.repository_session `
  --source eval_results/context-efficiency/repository-ab `
  --output eval_results/context-efficiency/repository-session `
  --issues 4 --parallel 1
```

This separately reported workload transplants compatible real issues onto one
fixed SymPy checkout. Every accepted combination passes the reference repair
precheck before model execution. Four combined issues are one derived task,
not four independent samples and not four official original-commit benchmark
instances. Incompatible candidates and their reasons remain in the artifacts.

For a session that receives new requirements one by one, prepare a sequential
follow-up workload. The runner keeps one Agent and one checkout while sending
the four issue requests as successive user turns:

The recommended long-session data is fixed in
`evals/context_efficiency/real_long_task_manifest.json`. It contains the four
issues that were already proven compatible on one SymPy checkout, in a fixed
stage order. Prepare it separately from the independent repair set:

```powershell
uv run python -m evals.context_efficiency.repository_session `
  --source eval_results/context-efficiency/repository-ab `
  --output eval_results/context-efficiency/repository-long-v1 `
  --manifest evals/context_efficiency/real_long_task_manifest.json `
  --prepare-only
```

Only after this combination preflight succeeds should it be passed to the
sequential follow-up runner. This workload is intended to measure context
growth, compression triggers, cache behavior and preservation of earlier
repairs; its end-to-end code-fix success is reported separately.

```powershell
uv run python -m evals.context_efficiency.repository_followup `
  --source eval_results/context-efficiency/repository-ab `
  --combined eval_results/context-efficiency/repository-long-v1 `
  --output eval_results/context-efficiency/repository-long-followup-v1
```

The follow-up workload is frozen during preparation and is reported separately
from the combined single-prompt workload. It was prepared during the current
evaluation, while Provider availability prevented spending another live quota
window on its three repetitions.

The current real-repository artifacts are:

- `docs/CONTEXT_COMPRESSION_REPOSITORY_EXPERIMENT.md`: 63 completed attempts
  from a 20-issue Flask/SymPy selection; unfinished slots remain in the raw
  directory and are not counted as successes.
- `docs/CONTEXT_COMPRESSION_REPOSITORY_SESSION_EXPERIMENT.md`: 6 completed
  attempts for the four-issue SymPy maintenance workload.
- `eval_results/context-efficiency/repository-ab/real-repository-outcomes.png`:
  outcomes, paired input changes, cache ratios and compression activity.

## Resuming and data integrity

Reusing an output directory never retries completed failures. Runtime code,
task IDs and semantic configuration remain frozen. Concurrency may change,
with each execution phase recorded. Interrupted attempts retain their entire
checkout, requests and partial work under `interrupted/`; restarting an
interrupted slot creates a new physical attempt. Interruption counts and their
reported usage are separately disclosed.

Account exhaustion and invalid Provider usage stop new dispatch. A nonempty
request with zero total input is not valid measurement; fully cached input is
valid. Missing usage is not replaced by a favourable zero. Independent scoring
corrections are stored separately from the original terminal record.

```powershell
# Refresh measurement-quality assessments without sending model requests.
uv run python -m evals.context_efficiency.repository_ab `
  --output eval_results/context-efficiency/repository-ab --report-only

# Repair the historical patch-capture grading issue without rerunning models.
uv run python -m evals.context_efficiency.repository_ab `
  --output eval_results/context-efficiency/repository-session --rescore --report-only

# A small endpoint probe; no tool returned by the model is executed.
uv run python -m evals.context_efficiency.provider_health `
  --output eval_results/context-efficiency/repository-ab/health/manual.json

# Generate the readable report from report.json and selection.json.
uv run python -m evals.context_efficiency.repository_report `
  --results eval_results/context-efficiency/repository-ab `
  --output docs/CONTEXT_COMPRESSION_REPOSITORY_EXPERIMENT.md
```

The output includes source/dependency provenance, eligibility checks, full
request envelopes, usage, tool events, patches, grading XML and failure logs.
Requests can contain repository source code: retain them according to the
repository's own data policy rather than publishing raw traces indiscriminately.

## Interpreting the result

Read task success, service failures and usage quality before token reduction.
Failed attempts remain in the end-to-end denominator; early failures can
artificially lower token use. Both-successful comparisons are supplemental,
not a replacement for the full population. Include summary requests and cached
input in total input. Do not convert input reduction to cost reduction without
verified endpoint prices. Without a compression event, execution-path variation
is not measured compression benefit. The 32K pressure profile is not the
default 200K production profile.
