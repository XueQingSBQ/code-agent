"""Rules system: conditional project-specific behavior."""

from carbide.rules.base import (
    Rule,
    RuleCondition,
    RuleContext,
    RuleRegistry,
    RuleTrigger,
    RuleWriter,
    create_rule_template,
)
from carbide.rules.manager import RuleManager, create_default_rules

__all__ = [
    "Rule",
    "RuleCondition",
    "RuleContext",
    "RuleManager",
    "RuleRegistry",
    "RuleTrigger",
    "RuleWriter",
    "create_default_rules",
    "create_rule_template",
]
