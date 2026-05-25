"""Regression tests for #322: auto-discover ``creek_config.yaml`` from ``--vault``.

When a CLI command is invoked with ``--vault <path>`` and
``<path>/00-Creek-Meta/creek_config.yaml`` exists, the pipeline must
load that config automatically — without requiring the operator to also
set ``CREEK_CONFIG`` or pass ``--config``.

The "config not found" warning emitted by
:func:`creek.config.load_config` should only fire when none of those
three discovery paths produce a real file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml
from typer.testing import CliRunner

from creek.cli import app
from creek.config import CONFIG_PATH_ENV_VAR, resolve_config_path

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


# ---------------------------------------------------------------------------
# resolve_config_path(): the pure helper
# ---------------------------------------------------------------------------


class TestResolveConfigPath:
    """Tests for :func:`creek.config.resolve_config_path`."""

    def test_explicit_wins_over_env_and_vault(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An explicit ``--config`` path beats both env var and vault discovery."""
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text("")
        vault = tmp_path / "vault"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        (vault / "00-Creek-Meta" / "creek_config.yaml").write_text("")
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "env.yaml"))

        assert resolve_config_path(vault, explicit) == explicit

    def test_env_var_wins_over_vault(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``CREEK_CONFIG`` beats vault auto-discovery when ``explicit`` is None."""
        env_path = tmp_path / "env.yaml"
        env_path.write_text("")
        vault = tmp_path / "vault"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        (vault / "00-Creek-Meta" / "creek_config.yaml").write_text("")
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(env_path))

        assert resolve_config_path(vault, None) == env_path

    def test_vault_discovery_when_no_env_or_explicit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A vault with a real config is discovered when nothing else is set."""
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        vault = tmp_path / "vault"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        candidate = vault / "00-Creek-Meta" / "creek_config.yaml"
        candidate.write_text("")

        assert resolve_config_path(vault, None) == candidate

    def test_vault_without_config_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A vault with no config file leaves discovery empty."""
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        vault = tmp_path / "vault"
        vault.mkdir()

        assert resolve_config_path(vault, None) is None

    def test_no_vault_no_env_no_explicit_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no inputs at all, the helper returns ``None``."""
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        assert resolve_config_path(None, None) is None

    def test_empty_env_var_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An empty/whitespace env var behaves as if unset (matches load_config)."""
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, "   ")
        vault = tmp_path / "vault"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        candidate = vault / "00-Creek-Meta" / "creek_config.yaml"
        candidate.write_text("")

        assert resolve_config_path(vault, None) == candidate


# ---------------------------------------------------------------------------
# CLI end-to-end: redact --scan --vault <path> --source <path>
# ---------------------------------------------------------------------------


def _scaffold_initialised_vault(vault: Path) -> Path:
    """Return *vault* after running ``creek init --vault <vault>``."""
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    config_path = vault / "00-Creek-Meta" / "creek_config.yaml"
    assert config_path.exists()
    return config_path


def test_redact_scan_with_vault_does_not_warn_about_missing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``creek redact --scan`` finds the vault's config from --vault (issue #322)."""
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    _scaffold_initialised_vault(vault)
    source = tmp_path / "src"
    source.mkdir()
    (source / "note.md").write_text("hello world\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        result = runner.invoke(
            app,
            [
                "redact",
                "--scan",
                "--vault",
                str(vault),
                "--source",
                str(source),
            ],
        )

    assert result.exit_code == 0, result.output
    assert not any(
        "not found" in r.message and "config" in r.message.lower()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_redact_scan_without_vault_still_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``--vault`` and no env/explicit ⇒ warning still fires (preserved contract)."""
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)  # cwd has no creek_config.yaml
    source = tmp_path / "src"
    source.mkdir()
    (source / "note.md").write_text("hello world\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        result = runner.invoke(app, ["redact", "--scan", "--source", str(source)])

    assert result.exit_code == 0, result.output
    assert any(
        "not found" in r.message for r in caplog.records if r.levelno == logging.WARNING
    )


def test_redact_scan_env_var_overrides_vault_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``CREEK_CONFIG`` pointing at a missing path still raises, even with --vault."""
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    _scaffold_initialised_vault(vault)
    source = tmp_path / "src"
    source.mkdir()
    (source / "note.md").write_text("hello world\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "nonexistent.yaml"))

    result = runner.invoke(
        app,
        [
            "redact",
            "--scan",
            "--vault",
            str(vault),
            "--source",
            str(source),
        ],
    )

    # Explicit env override pointing at a missing file is a hard error,
    # matching the documented contract of load_config(). The vault's
    # config must NOT silently take over.
    assert result.exit_code != 0


def test_classify_with_vault_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``creek classify --vault <vault>`` discovers the vault's config silently."""
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    config_path = _scaffold_initialised_vault(vault)

    # Edit the vault's config so we can later assert the override path
    # would also load values from the file (no built-in defaults).
    data = yaml.safe_load(config_path.read_text()) or {}
    data["classification"] = {"confidence_threshold": 0.42}
    config_path.write_text(yaml.dump(data))

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        result = runner.invoke(app, ["classify", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert not any(
        "not found" in r.message for r in caplog.records if r.levelno == logging.WARNING
    )
