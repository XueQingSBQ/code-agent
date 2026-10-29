"""Bash command safety analysis.

M2 bashguard: pattern-based detection of dangerous shell constructs.
M3 will add full AST parsing via bashlex for precise analysis.

This module flags commands that:
- Delete files recursively (rm -rf, find -delete)
- Overwrite system files (/etc, /usr, /sys)
- Execute downloaded code (curl | sh, wget -O- | bash)
- Modify permissions broadly (chmod -R 777)
- Use shell metacharacters that enable injection
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


class Severity(Enum):
    """Severity level of a detected issue."""

    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"


@dataclass
class Finding:
    """A detected safety issue in a command."""

    severity: Severity
    message: str
    pattern: str


class BashGuard:
    """Pattern-based bash command analyzer."""

    # Patterns for destructive operations
    DESTRUCTIVE_PATTERNS: ClassVar[list[tuple[str, Severity, str]]] = [
        (
            r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)*-[a-zA-Z]*f",
            Severity.DANGER,
            "recursive force delete (rm -rf)",
        ),
        (r"\bdd\s+if=/dev/zero", Severity.DANGER, "disk wipe with dd"),
        (r":\(\)\s*\{[^}]*:\|:&\s*\}", Severity.DANGER, "fork bomb detected"),
        (r"\bfind\b.*-delete", Severity.DANGER, "find with -delete"),
        (r"\bchmod\s+-R\s+777", Severity.DANGER, "recursive 777 permissions"),
        (
            r"\b(curl|wget)\b[^|]*\|\s*(bash|sh)",
            Severity.DANGER,
            "pipe downloaded content to shell",
        ),
    ]

    # Patterns for system file access
    SYSTEM_PATHS: ClassVar[list[tuple[str, Severity, str]]] = [
        (r"(/etc|/usr|/sys|/boot|/var/lib)/", Severity.WARNING, "accessing system directories"),
        (r">\s*/dev/[a-z]+", Severity.WARNING, "writing to device files"),
    ]

    # Shell injection risk patterns
    INJECTION_PATTERNS: ClassVar[list[tuple[str, Severity, str]]] = [
        (r"\$\([^)]*\$\(", Severity.WARNING, "nested command substitution"),
        (r"[;&|]\s*\$", Severity.INFO, "variable after shell metacharacter"),
    ]

    def analyze(self, command: str) -> list[Finding]:
        """Analyze a command and return all findings."""
        findings: list[Finding] = []

        for pattern, severity, message in self.DESTRUCTIVE_PATTERNS:
            if re.search(pattern, command):
                findings.append(Finding(severity=severity, message=message, pattern=pattern))

        for pattern, severity, message in self.SYSTEM_PATHS:
            if re.search(pattern, command):
                findings.append(Finding(severity=severity, message=message, pattern=pattern))

        for pattern, severity, message in self.INJECTION_PATTERNS:
            if re.search(pattern, command):
                findings.append(Finding(severity=severity, message=message, pattern=pattern))

        return findings

    def is_safe(self, command: str) -> bool:
        """Quick check: returns False if any DANGER-level finding exists."""
        findings = self.analyze(command)
        return all(f.severity != Severity.DANGER for f in findings)
