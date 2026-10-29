# SWE-bench Lite Evaluation

This directory contains the evaluation harness for running Carbide on [SWE-bench Lite](https://www.swebench.com/), a benchmark of 300 real GitHub issues from popular Python repositories.

## Setup

### Prerequisites

1. **Docker** - Required for isolated test execution
   - Windows: Docker Desktop with WSL2 backend
   - Linux/Mac: Docker Engine

2. **SWE-bench Lite Dataset** - Download from the official repository:
   ```bash
   wget https://raw.githubusercontent.com/princeton-nlp/SWE-bench/main/swebench/harness/constants.py
   # Or use the official dataset release
   ```

3. **API Access** - Anthropic API key or compatible endpoint

### Installation

```bash
# Install Carbide with evaluation dependencies
cd /path/to/carbide
uv pip install -e .
```

## Running Evaluations

### Quick Test (10 instances)

```bash
python -m evals.swebench.cli \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results \
    --api-key $ANTHROPIC_API_KEY \
    --model claude-sonnet-4-20250514 \
    --max-instances 10
```

### Full Evaluation (300 instances)

⚠️ **Cost Warning**: A full run with Claude Sonnet can cost $50-200 depending on task complexity.

```bash
python -m evals.swebench.cli \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results_full \
    --api-key $ANTHROPIC_API_KEY \
    --model claude-sonnet-4-20250514 \
    --parallel 4
```

### Using Custom API Endpoints

```bash
python -m evals.swebench.cli \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results \
    --api-key sk-your-key \
    --base-url https://your-endpoint.com \
    --model claude-sonnet-4-6 \
    --max-instances 10
```

## Output Structure

```
eval_results/
├── aggregate_report.json          # Summary metrics
├── instance_id_1.json             # Per-instance detailed metrics
├── instance_id_2.json
├── ...
└── instance_id_N/                 # Working directory for each instance
    └── repo/                      # Cloned repository
```

## Metrics

### Per-Instance Metrics

- **resolved**: Whether the issue was successfully fixed
- **tokens**: Prompt, completion, and cached token counts
- **cost_usd**: Estimated cost based on model pricing
- **duration_seconds**: Wall-clock time
- **num_turns**: Number of agent-model interactions
- **num_tool_calls**: Total tool invocations
- **tests_passed/failed**: Test results after applying the fix

### Aggregate Metrics

- **resolve_rate**: Percentage of successfully resolved issues
- **avg_cost_per_instance**: Average cost per instance
- **avg_tokens_per_instance**: Average token consumption
- **total_cost_usd**: Total evaluation cost

## Architecture

### Components

- **runner.py**: Main evaluation loop and orchestration
- **instance.py**: SWE-bench instance loading and formatting
- **docker.py**: Container management for isolated execution
- **metrics.py**: Token and cost tracking
- **cli.py**: Command-line interface

### Evaluation Flow

1. Load SWE-bench Lite instances from dataset
2. For each instance:
   - Create isolated Docker container
   - Clone repository at correct commit
   - Run Carbide agent with problem statement
   - Apply test patch
   - Run test suite
   - Collect metrics (tokens, cost, time)
   - Clean up container
3. Aggregate results and generate report

## WSL2 Setup (Windows)

```bash
# Enable WSL2
wsl --install

# Install Docker Desktop with WSL2 backend
# Download from: https://www.docker.com/products/docker-desktop

# Verify Docker works in WSL2
wsl
docker run hello-world
```

## Cost Estimation

Based on Claude Sonnet 4 pricing (as of implementation):
- Input: $3 / 1M tokens
- Output: $15 / 1M tokens  
- Cached input: $0.30 / 1M tokens

Average per instance (estimated):
- 50-200 turns
- 100K-500K tokens
- $0.50-$2.00 per instance

Full benchmark (300 instances):
- **Total: $150-$600**

Use `--max-instances 10-20` for development to control costs.

## CI Integration

For continuous evaluation, add a nightly GitHub Action:

```yaml
name: SWE-bench Evaluation
on:
  schedule:
    - cron: '0 2 * * *'  # 2 AM daily
  workflow_dispatch:

jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: docker/setup-buildx-action@v2
      - name: Run evaluation subset
        run: |
          python -m evals.swebench.cli \
            --dataset ./data/swebench_lite_subset.json \
            --output ./results \
            --api-key ${{ secrets.ANTHROPIC_API_KEY }} \
            --max-instances 20
      - name: Upload results
        uses: actions/upload-artifact@v3
        with:
          name: eval-results
          path: ./results/aggregate_report.json
```

## Troubleshooting

### Docker Permission Errors

```bash
# Linux: Add user to docker group
sudo usermod -aG docker $USER
newgrp docker
```

### Out of Memory

Increase Docker memory limit in Docker Desktop settings (8GB+ recommended).

### Network Timeouts

Increase timeout with `--timeout` flag (default: 1800s).
The value is applied independently to every instance; invalid timeout and
parallelism values are rejected before any Docker container is created.

Each container is also started with explicit resource ceilings. The defaults
are 2 CPUs, 4 GiB memory, and 512 processes; tune them with
`--docker-cpus`, `--docker-memory`, and `--docker-pids-limit` when the host
capacity or benchmark repository requires it.
