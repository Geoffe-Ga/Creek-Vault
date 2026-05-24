"""Regression tests for INC-008: ``CREEK_CONFIG`` env-var discovery in ``load_config``.

The MCP server previously discovered ``creek_config.yaml`` only from
the current working directory, so subprocesses launched from a non-vault
cwd (e.g. CrawDad spawning ``creek-tools-mcp``) silently fell back to
built-in defaults. INC-008 adds a ``CREEK_CONFIG`` env-var fallback
to :func:`creek.config.load_config` so any inherited environment
delivers the operator's real configuration to every caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import yaml

from creek.config import load_config

if TYPE_CHECKING:
    from pathlib import Path


def test_load_config_honors_creek_config_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``config_path is None`` + ``CREEK_CONFIG`` set → that path wins."""
    config_file = tmp_path / "vault" / "creek_config.yaml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        yaml.dump({"llm": {"provider": "anthropic", "model": "claude-sonnet-4-6"}}),
    )
    monkeypatch.setenv("CREEK_CONFIG", str(config_file))
    monkeypatch.chdir(tmp_path)  # ensure no ./creek_config.yaml masks the env var

    cfg = load_config()

    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-sonnet-4-6"


def test_load_config_env_var_nonexistent_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured ``CREEK_CONFIG`` raises, not silent defaults."""
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("CREEK_CONFIG", str(missing))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="CREEK_CONFIG"):
        load_config()


def test_load_config_explicit_path_overrides_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``config_path`` wins over the env var."""
    env_file = tmp_path / "env.yaml"
    env_file.write_text(yaml.dump({"llm": {"provider": "ollama"}}))
    explicit_file = tmp_path / "explicit.yaml"
    explicit_file.write_text(yaml.dump({"llm": {"provider": "anthropic"}}))
    monkeypatch.setenv("CREEK_CONFIG", str(env_file))

    cfg = load_config(explicit_file)

    assert cfg.llm.provider == "anthropic"


def test_load_config_warning_mentions_creek_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The discovery-failure warning names ``CREEK_CONFIG``."""
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        load_config()

    assert any("CREEK_CONFIG" in r.message for r in caplog.records)


def test_load_config_empty_env_var_falls_through_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty ``CREEK_CONFIG`` is treated like an unset one."""
    monkeypatch.setenv("CREEK_CONFIG", "")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        cfg = load_config()

    assert cfg.vault_path.name == ""  # defaults preserved
    # And the warning still fires — we're in the "neither set" path.
    assert any("not found" in r.message for r in caplog.records)
