# Contributing to Carbide

Thank you for your interest in contributing to Carbide! This document provides guidelines and information for contributors.

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- Docker (for running SWE-bench evaluations)
- Git

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/XueQingSBQ/code-agent.git
cd code-agent

# Install dependencies
uv sync

# Run tests to verify setup
uv run pytest

# Run linters
uv run ruff check .
uv run mypy
```

## Project Structure

```
carbide/
├── src/carbide/          # Main source code
│   ├── engine/           # Query loop, tool executor, repair
│   ├── context/          # Five-layer compression pyramid
│   ├── tools/            # Tool implementations
│   ├── governance/       # Permission engine, BashGuard
│   ├── providers/        # Model provider abstraction
│   ├── infra/            # Config, logging, storage
│   ├── cli/              # CLI interface
│   └── sdk.py            # Public SDK API
├── tests/                # Test suite
├── evals/                # Evaluation harnesses
│   └── swebench/         # SWE-bench Lite runner
├── docs/                 # Documentation
└── pyproject.toml        # Project configuration
```

## Coding Standards

### Style Guide

- Follow PEP 8 (enforced by Ruff)
- Line length: 100 characters
- Use type hints everywhere (`mypy --strict` must pass)
- Docstrings for public APIs (Google style)

### Type Checking

Carbide uses strict mypy:

```bash
uv run mypy
```

All code must pass with no errors. Common patterns:

```python
# Use explicit types
def process(data: list[str]) -> dict[str, int]:
    ...

# Handle None explicitly
def maybe_value(x: str | None) -> str:
    if x is None:
        return "default"
    return x
```

### Import Order

Ruff enforces import sorting (isort-compatible):

1. Standard library
2. Third-party packages
3. Local imports

```python
import os
from pathlib import Path

import anthropic
from pydantic import BaseModel

from carbide.engine import query_loop
from carbide.tools import ToolRegistry
```

## Architecture Constraints

### Layer Dependencies

Dependencies flow **downward only**. This is enforced by `import-linter`:

```
cli/sdk → engine → context/tools → providers/infra
```

**Forbidden**:
- Tools importing from engine
- Engine importing from CLI
- Events importing from anything

Run the check:

```bash
uv run lint-imports
```

### Provider Seam

Only `src/carbide/providers/` may import vendor SDKs (e.g., `anthropic`).

**Why?** Prevents vendor lock-in. The engine talks to a `Provider` protocol, not to Anthropic specifically.

## Testing

### Running Tests

```bash
# All tests
uv run pytest

# With coverage
uv run pytest --cov=src/carbide --cov-report=html

# Specific test file
uv run pytest tests/engine/test_executor.py

# Skip live API tests (default)
uv run pytest -m "not live"

# Run live API tests (requires ANTHROPIC_API_KEY)
uv run pytest -m live
```

### Test Categories

1. **Unit tests**: Test individual components
   - Fast (< 1s each)
   - No external dependencies
   - Mock all I/O

2. **Property tests**: Test invariants over generated inputs
   - Use Hypothesis
   - Example: message history repair

3. **Integration tests**: Test component interactions
   - Use MockProvider
   - Deterministic fixtures

4. **Live tests**: Test against real API
   - Marked with `@pytest.mark.live`
   - Excluded from default run
   - Run in CI nightly only

### Writing Tests

#### Unit Test Example

```python
import pytest
from carbide.tools.fs.read import ReadTool

@pytest.mark.asyncio
async def test_read_tool_basic():
    tool = ReadTool()
    result = await tool.execute(file_path="README.md", limit=10)
    assert len(result.split("\n")) <= 10
```

#### Property Test Example

```python
from hypothesis import given, strategies as st
from carbide.engine.repair import repair_orphan_tool_uses

@given(st.lists(st.dictionaries(st.text(), st.text())))
def test_repair_preserves_invariant(messages):
    repaired = repair_orphan_tool_uses(messages)
    assert all_tool_uses_have_results(repaired)
```

#### Integration Test Example

```python
from carbide.engine.loop import query_loop
from tests.fixtures.mock_provider import MockProvider

@pytest.mark.asyncio
async def test_query_loop_handles_tool_error():
    provider = MockProvider(fixture="tool_error.json")
    events = []
    
    async for event in query_loop(messages, provider, tools, state):
        events.append(event)
    
    assert any(e.type == "tool_error" for e in events)
```

## Adding a New Tool

### 1. Create the tool class

```python
# src/carbide/tools/your_category/your_tool.py
from carbide.tools.base import Tool

class YourTool(Tool):
    name = "your_tool"
    read_only = False        # Can use in plan mode?
    destructive = False      # Needs user approval?
    concurrency_safe = True  # Can run in parallel?
    
    async def execute(self, param1: str, param2: int = 10) -> str:
        """
        Tool description for the model.
        
        Args:
            param1: Description of param1
            param2: Description of param2
        
        Returns:
            Result description
        """
        # Implementation
        return "result"
```

### 2. Add JSON schema

Pydantic models automatically generate the schema:

```python
from pydantic import BaseModel, Field

class YourToolInput(BaseModel):
    param1: str = Field(..., description="Description of param1")
    param2: int = Field(10, description="Description of param2")
```

### 3. Register the tool

```python
# src/carbide/tools/registry.py
from .your_category.your_tool import YourTool

def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # ... existing tools ...
    registry.register(YourTool())
    return registry
```

### 4. Write tests

```python
# tests/tools/test_your_tool.py
import pytest
from carbide.tools.your_category.your_tool import YourTool

@pytest.mark.asyncio
async def test_your_tool_basic():
    tool = YourTool()
    result = await tool.execute(param1="test")
    assert result == "expected"

def test_your_tool_declares_safety_properties():
    """Verify tool contract compliance."""
    assert hasattr(YourTool, "name")
    assert hasattr(YourTool, "read_only")
    assert hasattr(YourTool, "destructive")
    assert hasattr(YourTool, "concurrency_safe")
```

## Pull Request Process

### Before Submitting

1. **Run the full test suite**: `uv run pytest`
2. **Check types**: `uv run mypy`
3. **Check style**: `uv run ruff check .`
4. **Check imports**: `uv run lint-imports`
5. **Update tests**: Add tests for new functionality
6. **Update docs**: Update relevant documentation

### PR Guidelines

- **One concern per PR**: Don't mix refactoring with new features
- **Clear description**: Explain what and why, not just how
- **Reference issues**: Link to related issues
- **Small diffs**: Easier to review, faster to merge
- **Tests included**: No PR without tests

### PR Template

```markdown
## Description
Brief description of changes

## Motivation
Why is this change needed?

## Changes
- Change 1
- Change 2

## Testing
How was this tested?

## Checklist
- [ ] Tests added/updated
- [ ] Types checked (mypy)
- [ ] Style checked (ruff)
- [ ] Import constraints verified (lint-imports)
- [ ] Documentation updated
```

## Evaluation Contributions

### Running Local Evals

```bash
# Small test run
python -m evals.swebench.cli \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results \
    --api-key $ANTHROPIC_API_KEY \
    --max-instances 5
```

### Adding New Evaluation Suites

1. Create directory under `evals/`
2. Implement runner following `evals/swebench/` pattern
3. Document in `evals/your_eval/README.md`
4. Add metrics to aggregate reporting

## Documentation

### What to Document

- **Public APIs**: Every public function/class
- **Architecture decisions**: Why, not just what
- **Non-obvious behavior**: Anything that surprised you
- **Performance considerations**: Known bottlenecks

### Where to Document

- **Docstrings**: For code-level documentation
- **README.md**: For getting started
- **docs/ARCHITECTURE.md**: For design decisions
- **evals/*/README.md**: For evaluation procedures

## Getting Help

- **Questions**: Open a GitHub Discussion
- **Bug reports**: Open a GitHub Issue with:
  - Minimal reproduction
  - Expected vs actual behavior
  - Environment details (OS, Python version)
- **Feature requests**: Open an Issue explaining:
  - Use case
  - Proposed solution
  - Alternatives considered

## License

By contributing to Carbide, you agree that your contributions will be licensed under the MIT License.
