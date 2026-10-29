"""Configuration with an explicit precedence chain.

Precedence, highest first: explicit constructor argument > environment
variable > project config file > user config file > built-in default.
A production tool needs this to be predictable and documented, not implicit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ShellBackend = Literal["gitbash", "wsl", "posix"]


class ContextConfig(BaseModel):
    """Thresholds for the five-layer pyramid.

    Absolute token counts, never percentages: when the context window grows,
    the budget should not silently grow with it.
    """

    # L1 spill
    spill_single_result_bytes: int = Field(default=50_000, gt=0)
    spill_message_batch_bytes: int = Field(default=200_000, gt=0)
    spill_preview_bytes: int = Field(default=2_000, gt=0)

    # L3 micro-compact
    micro_idle_seconds: float = Field(default=3_600.0, ge=0)
    micro_keep_recent: int = Field(default=5, ge=0)

    # L4 compact
    compact_buffer_tokens: int = Field(default=13_000, gt=0)
    summary_reserved_tokens: int = Field(default=20_000, gt=0)
    compact_max_failures: int = Field(default=3, gt=0)

    # L4.5 post-compact restore
    restore_max_files: int = Field(default=5, ge=0)
    restore_tokens_per_file: int = Field(default=5_000, gt=0)
    restore_token_budget: int = Field(default=50_000, gt=0)

    # Master switch, so M1 can measure pyramid-on vs pyramid-off.
    enabled: bool = True


class Config(BaseModel):
    api_key: str = Field(repr=False)
    base_url: str | None = None
    model: str = "claude-sonnet-5"
    side_model: str = "claude-haiku-4-5-20251001"
    context_window: int = Field(default=200_000, gt=0)
    max_output_tokens: int = Field(default=8_000, gt=0)
    escalated_max_output_tokens: int = Field(default=16_000, gt=0)
    # ``None`` is the normal production mode; a positive value is an
    # explicit safety valve for bounded or evaluation runs.
    max_turns: int | None = Field(default=None, gt=0)
    shell: ShellBackend = "gitbash"
    memory_write_mode: Literal["off", "explicit", "suggest", "auto"] = "auto"
    audit_max_bytes: int = Field(default=10_000_000, gt=0)
    audit_backup_count: int = Field(default=5, ge=0)
    audit_max_field_chars: int = Field(default=16_384, ge=128)
    workspace: Path = Field(default_factory=Path.cwd)
    context: ContextConfig = Field(default_factory=ContextConfig)

    @property
    def effective_context_window(self) -> int:
        """Window minus the room a summary needs to be written."""
        return self.context_window - self.context.summary_reserved_tokens

    @property
    def compact_threshold(self) -> int:
        """Token count at which L4 fires."""
        return self.effective_context_window - self.context.compact_buffer_tokens

    @classmethod
    def load(cls, workspace: Path | None = None, **overrides: object) -> Config:
        # Read project-local .env first, then overlay the real process
        # environment.  This keeps local development convenient while making
        # container/CI deployments behave as documented (environment wins).
        from dotenv import dotenv_values

        env_path = Path.cwd() / ".env"
        env_vars: dict[str, str | None] = dict(dotenv_values(env_path) if env_path.exists() else {})
        env_vars.update(os.environ)

        api_key = env_vars.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        values: dict[str, object] = {
            "api_key": api_key,
            "base_url": env_vars.get("ANTHROPIC_BASE_URL") or None,
            "workspace": (workspace or Path.cwd()).resolve(),
        }
        # Model: CARBIDE_MODEL takes precedence, fallback to ANTHROPIC_MODEL
        if model := env_vars.get("CARBIDE_MODEL") or env_vars.get("ANTHROPIC_MODEL"):
            values["model"] = model
        if side := env_vars.get("CARBIDE_SIDE_MODEL"):
            values["side_model"] = side
        if shell := env_vars.get("CARBIDE_SHELL"):
            values["shell"] = shell
        if memory_mode := env_vars.get("CARBIDE_MEMORY_WRITE_MODE"):
            values["memory_write_mode"] = memory_mode
        if audit_max_bytes := env_vars.get("CARBIDE_AUDIT_MAX_BYTES"):
            values["audit_max_bytes"] = audit_max_bytes
        if audit_backup_count := env_vars.get("CARBIDE_AUDIT_BACKUP_COUNT"):
            values["audit_backup_count"] = audit_backup_count
        if audit_max_field_chars := env_vars.get("CARBIDE_AUDIT_MAX_FIELD_CHARS"):
            values["audit_max_field_chars"] = audit_max_field_chars
        values.update(overrides)
        return cls.model_validate(values)
