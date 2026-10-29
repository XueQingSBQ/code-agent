"""Configuration precedence and validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from carbide.infra.config import Config, ContextConfig


def test_config_prefers_process_environment_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=dotenv-key\nCARBIDE_MODEL=dotenv-model\nCARBIDE_AUDIT_MAX_BYTES=1000\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "process-key")
    monkeypatch.setenv("CARBIDE_MODEL", "process-model")
    monkeypatch.setenv("CARBIDE_AUDIT_MAX_BYTES", "2000")

    config = Config.load(workspace=tmp_path)

    assert config.api_key == "process-key"
    assert config.model == "process-model"
    assert config.audit_max_bytes == 2000


def test_config_reads_audit_limits_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=test-key\nCARBIDE_AUDIT_BACKUP_COUNT=7\n"
        "CARBIDE_AUDIT_MAX_FIELD_CHARS=4096\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CARBIDE_AUDIT_BACKUP_COUNT", raising=False)
    monkeypatch.delenv("CARBIDE_AUDIT_MAX_FIELD_CHARS", raising=False)

    config = Config.load(workspace=tmp_path)

    assert config.audit_backup_count == 7
    assert config.audit_max_field_chars == 4096


def test_config_rejects_invalid_resource_limits() -> None:
    with pytest.raises(ValidationError):
        Config(api_key="test", context_window=0)
    with pytest.raises(ValidationError):
        ContextConfig(restore_max_files=-1)
    with pytest.raises(ValidationError):
        Config(api_key="test", audit_backup_count=-1)
