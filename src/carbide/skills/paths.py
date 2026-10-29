"""Filesystem locations for Carbide-owned skill assets."""

from __future__ import annotations

import os
from pathlib import Path


def carbide_home() -> Path:
    """Return the user-level Carbide home directory.

    ``CARBIDE_HOME`` is intentionally separate from the current workspace:
    skills are capabilities of the agent installation, not project source
    files.  The environment override also gives tests and managed
    installations a deterministic location.
    """

    configured = os.environ.get("CARBIDE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".carbide").resolve()


def agent_skills_dir() -> Path:
    """Return the directory containing globally installed Agent skills."""

    configured = os.environ.get("CARBIDE_SKILLS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return carbide_home() / "skills"
