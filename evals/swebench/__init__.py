"""
SWE-bench Lite evaluation runner for Carbide.

This module provides infrastructure to evaluate Carbide on the SWE-bench Lite benchmark,
which consists of 300 real GitHub issues from popular Python repositories.

Key components:
- runner.py: Main evaluation loop
- docker.py: Container management for isolated execution
- instance.py: Task instance loading and validation
- evaluator.py: Solution verification against test suites
- metrics.py: Cost and performance tracking
"""
