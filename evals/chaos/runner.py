"""
Chaos testing framework for fault injection and recovery validation.

Tests the M2 claim: ≥95% session recovery rate under fault injection.
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class FaultType(Enum):
    """Types of faults to inject."""

    RATE_LIMIT_429 = "rate_limit_429"  # 429 rate limit error
    PROMPT_TOO_LONG = "prompt_too_long"  # Context window exceeded
    STREAM_INTERRUPT = "stream_interrupt"  # Network interruption mid-stream
    OUTPUT_TRUNCATION = "output_truncation"  # Max tokens exceeded
    TOOL_TIMEOUT = "tool_timeout"  # Tool execution timeout
    PROCESS_KILL = "process_kill"  # SIGKILL mid-turn


@dataclass
class ChaosTest:
    """A single chaos test scenario."""

    id: str
    fault_type: FaultType
    description: str
    injection_point: str  # When to inject (e.g., "turn_3", "after_tool_2")


# Test scenarios for each fault type
CHAOS_SCENARIOS = [
    # Rate limiting
    ChaosTest(
        id="rate_limit_early",
        fault_type=FaultType.RATE_LIMIT_429,
        description="Inject 429 on turn 2",
        injection_point="turn_2",
    ),
    ChaosTest(
        id="rate_limit_mid",
        fault_type=FaultType.RATE_LIMIT_429,
        description="Inject 429 after 3 successful turns",
        injection_point="turn_4",
    ),
    # Context overflow
    ChaosTest(
        id="prompt_overflow_gradual",
        fault_type=FaultType.PROMPT_TOO_LONG,
        description="Trigger prompt_too_long after building up context",
        injection_point="turn_5",
    ),
    # Stream interruption
    ChaosTest(
        id="stream_interrupt_text",
        fault_type=FaultType.STREAM_INTERRUPT,
        description="Cut stream during text generation",
        injection_point="mid_text_chunk",
    ),
    ChaosTest(
        id="stream_interrupt_tool",
        fault_type=FaultType.STREAM_INTERRUPT,
        description="Cut stream during tool_use generation",
        injection_point="mid_tool_use",
    ),
    # Output truncation
    ChaosTest(
        id="output_truncation_single",
        fault_type=FaultType.OUTPUT_TRUNCATION,
        description="Hit max_tokens in a single response",
        injection_point="turn_2",
    ),
    # Tool timeout
    ChaosTest(
        id="tool_timeout_bash",
        fault_type=FaultType.TOOL_TIMEOUT,
        description="Bash command hangs and times out",
        injection_point="bash_execution",
    ),
    ChaosTest(
        id="tool_timeout_read",
        fault_type=FaultType.TOOL_TIMEOUT,
        description="Read tool times out on large file",
        injection_point="read_execution",
    ),
    # Process kill
    ChaosTest(
        id="process_kill_mid_turn",
        fault_type=FaultType.PROCESS_KILL,
        description="SIGKILL during model streaming",
        injection_point="turn_3",
    ),
    ChaosTest(
        id="process_kill_during_tool",
        fault_type=FaultType.PROCESS_KILL,
        description="SIGKILL during tool execution",
        injection_point="tool_execution",
    ),
]


@dataclass
class ChaosResult:
    """Result of a single chaos test run."""

    test_id: str
    fault_type: FaultType
    recovered: bool  # Did the session recover?
    history_intact: bool  # Is message history invariant preserved?
    recovery_time_seconds: float
    error_message: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class ChaosReport:
    """Aggregate results from chaos testing."""

    results: list[ChaosResult]

    @property
    def recovery_rate(self) -> float:
        """Calculate recovery rate."""
        if not self.results:
            return 0.0
        recovered = sum(1 for r in self.results if r.recovered)
        return recovered / len(self.results)

    @property
    def history_violation_count(self) -> int:
        """Count history invariant violations."""
        return sum(1 for r in self.results if not r.history_intact)

    def recovery_rate_by_fault(self, fault_type: FaultType) -> float:
        """Recovery rate for specific fault type."""
        relevant = [r for r in self.results if r.fault_type == fault_type]
        if not relevant:
            return 0.0
        recovered = sum(1 for r in relevant if r.recovered)
        return recovered / len(relevant)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "overall_recovery_rate": f"{self.recovery_rate:.1%}",
            "history_violations": self.history_violation_count,
            "target": "≥95% recovery, 0 violations",
            "by_fault_type": {
                fault_type.value: f"{self.recovery_rate_by_fault(fault_type):.1%}"
                for fault_type in FaultType
            },
            "results": [
                {
                    "test_id": r.test_id,
                    "fault_type": r.fault_type.value,
                    "recovered": r.recovered,
                    "history_intact": r.history_intact,
                    "recovery_time": f"{r.recovery_time_seconds:.2f}s",
                    "error": r.error_message,
                }
                for r in self.results
            ],
        }


class FaultInjector:
    """Injects faults into the agent execution."""

    def __init__(self, scenario: ChaosTest):
        self.scenario = scenario
        self.injected = False

    def should_inject(self, context: dict[str, Any]) -> bool:
        """Determine if fault should be injected at this point."""
        # Parse injection point
        if self.scenario.injection_point.startswith("turn_"):
            turn_num = int(self.scenario.injection_point.split("_")[1])
            return context.get("turn_number") == turn_num

        elif self.scenario.injection_point == "mid_text_chunk":
            return context.get("event_type") == "text_delta" and random.random() < 0.3

        elif self.scenario.injection_point == "mid_tool_use":
            return context.get("event_type") == "tool_use_start"

        elif self.scenario.injection_point == "bash_execution":
            return context.get("tool_name") == "bash"

        elif self.scenario.injection_point == "read_execution":
            return context.get("tool_name") == "read"

        elif self.scenario.injection_point == "tool_execution":
            return context.get("phase") == "tool_execution"

        return False

    async def inject(self) -> Exception:
        """Generate the appropriate exception for this fault type."""
        self.injected = True

        if self.scenario.fault_type == FaultType.RATE_LIMIT_429:
            # Simulate 429 rate limit
            from anthropic import RateLimitError

            return RateLimitError("Rate limit exceeded", response=None, body=None)

        elif self.scenario.fault_type == FaultType.PROMPT_TOO_LONG:
            # Simulate prompt too long
            from anthropic import BadRequestError

            return BadRequestError(
                "prompt is too long", response=None, body={"type": "prompt_too_long"}
            )

        elif self.scenario.fault_type == FaultType.STREAM_INTERRUPT:
            # Simulate network interruption
            return ConnectionError("Stream interrupted")

        elif self.scenario.fault_type == FaultType.OUTPUT_TRUNCATION:
            # This is handled by checking stop_reason, not an exception
            return None  # Signal truncation via metadata

        elif self.scenario.fault_type == FaultType.TOOL_TIMEOUT:
            # Simulate tool timeout
            return asyncio.TimeoutError("Tool execution timed out")

        elif self.scenario.fault_type == FaultType.PROCESS_KILL:
            # Simulate process termination
            raise SystemExit(1)

        return Exception(f"Unknown fault type: {self.scenario.fault_type}")


async def run_chaos_test(
    scenario: ChaosTest,
    task_prompt: str,
    workspace: Path,
    api_key: str,
    model: str,
) -> ChaosResult:
    """
    Run a single chaos test.

    Args:
        scenario: The chaos scenario to test
        task_prompt: Task to execute
        workspace: Working directory
        api_key: API key
        model: Model to use

    Returns:
        ChaosResult with recovery metrics
    """
    from carbide.sdk import Agent
    from carbide.engine.repair import validate_message_history

    start_time = datetime.now()
    injector = FaultInjector(scenario)

    recovered = False
    history_intact = True
    error_message = None

    try:
        agent = Agent(api_key=api_key, model=model, workspace=workspace)

        turn_number = 0
        async with agent.query(task_prompt) as events:
            async for event in events:
                # Track turn number
                if event.type == "model_response_start":
                    turn_number += 1

                # Check if we should inject fault
                context = {
                    "turn_number": turn_number,
                    "event_type": event.type,
                    "tool_name": getattr(event, "name", None),
                    "phase": "streaming" if event.type.startswith("text") else "tool_execution",
                }

                if injector.should_inject(context) and not injector.injected:
                    fault = await injector.inject()
                    if fault:
                        raise fault

        # If we get here without exception, attempt to resume
        # This simulates session recovery after interruption
        messages = agent.get_message_history()
        history_intact = validate_message_history(messages)

        # Try to continue the session
        async with agent.query("Continue the previous task") as events:
            async for event in events:
                pass  # Just verify we can continue

        recovered = True

    except SystemExit:
        # Process kill - check if we can resume from saved state
        try:
            # Load saved session
            agent_resumed = Agent(api_key=api_key, model=model, workspace=workspace)
            # TODO: Implement session resume from disk
            recovered = False
            error_message = "Process killed, session resume not implemented"
        except Exception as e:
            recovered = False
            error_message = f"Failed to resume: {str(e)}"

    except Exception as e:
        error_message = str(e)
        recovered = False

    end_time = datetime.now()
    recovery_time = (end_time - start_time).total_seconds()

    return ChaosResult(
        test_id=scenario.id,
        fault_type=scenario.fault_type,
        recovered=recovered,
        history_intact=history_intact,
        recovery_time_seconds=recovery_time,
        error_message=error_message,
    )


async def run_chaos_suite(
    scenarios: list[ChaosTest],
    repetitions: int,
    api_key: str,
    model: str,
    output_path: Path,
) -> ChaosReport:
    """
    Run full chaos testing suite.

    Args:
        scenarios: List of chaos scenarios
        repetitions: How many times to run each scenario
        api_key: API key
        model: Model to use
        output_path: Where to save results

    Returns:
        ChaosReport with aggregate metrics
    """
    import json

    print("=" * 60)
    print("CHAOS TESTING - FAULT INJECTION")
    print("=" * 60)
    print(f"Scenarios: {len(scenarios)}")
    print(f"Repetitions per scenario: {repetitions}")
    print(f"Total runs: {len(scenarios) * repetitions}")
    print()

    all_results = []
    workspace = Path("test_workspace/chaos_test")

    for scenario in scenarios:
        print(f"Testing: {scenario.id} ({scenario.fault_type.value})")
        print(f"  {scenario.description}")

        scenario_results = []
        for i in range(repetitions):
            result = await run_chaos_test(
                scenario=scenario,
                task_prompt="List files in this directory and read README.md",
                workspace=workspace,
                api_key=api_key,
                model=model,
            )
            scenario_results.append(result)
            status = "✓" if result.recovered else "✗"
            print(f"  Run {i + 1}/{repetitions}: {status}")

        all_results.extend(scenario_results)
        print()

    # Generate report
    report = ChaosReport(results=all_results)

    # Save results
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)

    # Print summary
    print("=" * 60)
    print("CHAOS TEST RESULTS")
    print("=" * 60)
    print(f"Overall recovery rate: {report.recovery_rate:.1%}")
    print(f"History violations: {report.history_violation_count}")
    print()
    print("By fault type:")
    for fault_type in FaultType:
        rate = report.recovery_rate_by_fault(fault_type)
        print(f"  {fault_type.value:20s}: {rate:.1%}")
    print()
    print(f"Target: ≥95% recovery, 0 violations")
    print(f"Result saved to: {output_path}")

    return report
