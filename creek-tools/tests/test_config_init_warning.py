"""Regression tests for ARCH-002: missing-config warning + ``creek init``.

A vault with no ``creek_config.yaml`` previously silently picked up the
built-in defaults. ARCH-002 requires:

* :func:`creek.config.load_config` emits a ``WARNING`` when the file
  is missing (suppressable via ``warn_on_missing=False``).
* ``creek init --vault <vault>`` writes a starter config, refusing to
  overwrite an existing file unless ``--force`` is passed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml
from typer.testing import CliRunner

from creek.cli import app
from creek.config import load_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def test_load_config_warns_on_missing_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing config logs a WARNING with the resolved path (ARCH-002)."""
    missing = tmp_path / "creek_config.yaml"

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        config = load_config(missing)

    assert config is not None  # falls back to defaults
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("not found" in r.message for r in warnings)
    assert any(str(missing) in r.message for r in warnings)


def test_load_config_silenced_when_warn_on_missing_false(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``warn_on_missing=False`` suppresses the WARNING (used by ``creek init``)."""
    missing = tmp_path / "creek_config.yaml"

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        load_config(missing, warn_on_missing=False)

    assert not any(
        r.levelno == logging.WARNING and "not found" in r.message
        for r in caplog.records
    )


def test_creek_init_writes_starter_config(tmp_path: Path) -> None:
    """``creek init --vault <vault>`` materialises a starter config."""
    vault = tmp_path / "vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])

    assert result.exit_code == 0
    config_path = vault / "00-Creek-Meta" / "creek_config.yaml"
    assert config_path.exists()
    # The starter is valid YAML with the expected top-level keys.
    data = yaml.safe_load(config_path.read_text())
    assert "classification" in data
    assert "redaction" in data


def test_creek_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """A pre-existing config is preserved unless ``--force`` is passed."""
    vault = tmp_path / "vault"
    config_dir = vault / "00-Creek-Meta"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "creek_config.yaml"
    config_path.write_text("# operator-edited\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--vault", str(vault)])

    assert result.exit_code == 1
    assert config_path.read_text() == "# operator-edited\n"


def test_creek_init_force_overwrites_existing(tmp_path: Path) -> None:
    """``--force`` rewrites the config from scratch."""
    vault = tmp_path / "vault"
    config_dir = vault / "00-Creek-Meta"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "creek_config.yaml"
    config_path.write_text("# old\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--vault", str(vault), "--force"])

    assert result.exit_code == 0
    rewritten = config_path.read_text()
    assert rewritten != "# old\n"
    assert "classification" in yaml.safe_load(rewritten)
