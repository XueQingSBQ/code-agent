"""Typed errors raised by the Todo service."""

from __future__ import annotations


class TodoError(Exception):
    """Base class for expected Todo failures."""

    code = "todo_error"


class TodoValidationError(TodoError):
    code = "todo_validation_error"


class TodoPlanExistsError(TodoError):
    code = "todo_plan_exists"


class TodoConflictError(TodoError):
    code = "todo_conflict"


class TodoDependencyError(TodoError):
    code = "todo_dependency_error"
