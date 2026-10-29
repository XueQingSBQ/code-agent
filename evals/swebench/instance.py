"""
SWE-bench Lite instance loader and validator.

Handles loading problem instances from the SWE-bench Lite dataset and
preparing them for evaluation.
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SWEBenchInstance:
    """A single SWE-bench Lite problem instance."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    test_patch: str
    patch: str  # Gold solution patch
    version: str
    environment_setup_commit: str
    FAIL_TO_PASS: list[str]
    PASS_TO_PASS: list[str]


def load_instances(dataset_path: Path) -> list[SWEBenchInstance]:
    """
    Load SWE-bench Lite instances from the dataset file.

    Args:
        dataset_path: Path to the SWE-bench Lite JSON file

    Returns:
        List of problem instances
    """
    with dataset_path.open(encoding="utf-8") as dataset_file:
        data = json.load(dataset_file)
    if not isinstance(data, list):
        raise ValueError("SWE-bench dataset must contain a JSON list")

    def parse_test_list(value: object, field_name: str, index: int) -> list[str]:
        """Accept native lists and the JSON-encoded lists used by SWE-bench exports."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Instance {index}: {field_name} is not valid JSON") from exc
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(test, str) for test in value):
            raise ValueError(f"Instance {index}: {field_name} must be a list of strings")
        return list(value)

    instances = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Instance {index}: expected an object")
        required = ("instance_id", "repo", "base_commit", "problem_statement")
        missing = [key for key in required if not isinstance(item.get(key), str) or not item[key]]
        if missing:
            raise ValueError(f"Instance {index}: missing required fields: {', '.join(missing)}")
        instance = SWEBenchInstance(
            instance_id=item["instance_id"],
            repo=item["repo"],
            base_commit=item["base_commit"],
            problem_statement=item["problem_statement"],
            hints_text=item.get("hints_text", ""),
            test_patch=item.get("test_patch", ""),
            patch=item.get("patch", ""),
            version=item.get("version", ""),
            environment_setup_commit=str(
                item.get("environment_setup_commit") or item["base_commit"]
            ),
            FAIL_TO_PASS=parse_test_list(item.get("FAIL_TO_PASS", []), "FAIL_TO_PASS", index),
            PASS_TO_PASS=parse_test_list(item.get("PASS_TO_PASS", []), "PASS_TO_PASS", index),
        )
        instances.append(instance)

    return instances


def format_task_prompt(instance: SWEBenchInstance) -> str:
    """
    Format a SWE-bench instance into a task prompt for the agent.

    Args:
        instance: The problem instance

    Returns:
        Formatted prompt string
    """
    prompt = f"""You are working on repository: {instance.repo}

The following issue has been reported:

{instance.problem_statement}
"""

    if instance.hints_text:
        prompt += f"""
Hints:
{instance.hints_text}
"""

    prompt += """
Please analyze the issue and implement a fix. The repository is already checked out at the correct commit.
"""

    return prompt
