"""Governance layer for secure tool execution.

This module provides:
- Permission engine for access control
- Hooks system for pre/post execution
- Bash guard for command analysis
- Audit logger for compliance
- Session store for persistence
"""

from carbide.governance.audit import AuditEntry, AuditLogger
from carbide.governance.bashguard import BashGuard, Severity
from carbide.governance.hooks import HookRegistry
from carbide.governance.permission import (
    PermissionEngine,
    PermissionMode,
    PermissionResult,
    RiskLevel,
)
from carbide.governance.session_store import SessionStore

__all__ = [
    "AuditEntry",
    "AuditLogger",
    "BashGuard",
    "HookRegistry",
    "PermissionEngine",
    "PermissionMode",
    "PermissionResult",
    "RiskLevel",
    "SessionStore",
    "Severity",
]
