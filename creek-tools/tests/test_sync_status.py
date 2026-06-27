"""`creek sync --status` + per-tick logging + per-source state (#680).

A real Tier-A run records per-source ingested counts in ``last-run.json``
(keeping the #676 top-level keys), `--status` renders them, and each tick emits
structured stdlib log lines.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import _sync_state_path, _write_sync_state, app
from creek.config import CreekConfig, SyncConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _vault_with_journal(tmp_path: Path, *, entries: int = 2) -> tuple[Path, Path]:
    """Scaffold a vault + a journal source with *entries* markdown files."""
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta/Processing-Log", "01-Fragments/Journal"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "src" / "personal" / "journal"
    journal.mkdir(parents=True)
    for i in range(entries):
        (journal / f"2026-06-{i + 1:02d}.md").write_text(
            f"---\ndate: 2026-06-{i + 1:02d}\n---\nEntry number {i}.\n",
            encoding="utf-8",
        )
    return vault, tmp_path / "src"


def _fake_config(vault: Path, source_drive: Path) -> CreekConfig:
    """Config with the journal source enabled."""
    return CreekConfig(
        vault_path=vault,
        source_drive=source_drive,
        sync=SyncConfig(sources={"journal": True}),
    )


# ---- _write_sync_state (unit) ------------------------------------------


class TestStateSchema:
    """State keeps the #676 keys and adds a merging per-source block."""

    def test_keeps_top_level_keys(self, tmp_path: Path) -> None:
        """The #676 tier/at/dry_run keys still round-trip (backward-compat)."""
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_sync_state(vault, "A", dry_run=False, source_counts={"journal": 3})
        state = json.loads(_sync_state_path(vault).read_text(encoding="utf-8"))
        assert state["tier"] == "A"
        assert "at" in state
        assert state["dry_run"] is False
        assert state["sources"]["journal"]["ingested"] == 3

    def test_merges_prior_sources(self, tmp_path: Path) -> None:
        """A run that touches one source preserves the others' last entry."""
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_sync_state(vault, "A", dry_run=False, source_counts={"journal": 2})
        _write_sync_state(vault, "A", dry_run=False, source_counts={"gdrive": 0})
        sources = json.loads(_sync_state_path(vault).read_text())["sources"]
        assert sources["journal"]["ingested"] == 2  # preserved across runs
        assert sources["gdrive"]["ingested"] == 0


# ---- --status -----------------------------------------------------------


class TestStatusCommand:
    """`--status` prints the per-source last-run table."""

    def test_status_after_tier_a(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a real Tier-A run, --status shows the journal ingested count."""
        vault, src = _vault_with_journal(tmp_path, entries=2)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: _fake_config(vault, src)
        )
        r1 = runner.invoke(app, ["sync", "--tier", "A", "--vault", str(vault)])
        assert r1.exit_code == 0, r1.output

        # Backward-compat: the #676 keys still parse.
        state = json.loads(_sync_state_path(vault).read_text(encoding="utf-8"))
        assert state["tier"] == "A"
        assert state["sources"]["journal"]["ingested"] == 2

        result = runner.invoke(app, ["sync", "--status", "--vault", str(vault)])
        assert result.exit_code == 0, result.output
        assert "journal" in result.output
        assert "2" in result.output

    def test_status_with_no_prior_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--status before any run says so cleanly."""
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig(vault_path=vault)
        )
        result = runner.invoke(app, ["sync", "--status", "--vault", str(vault)])
        assert result.exit_code == 0, result.output
        assert "no sync has run" in result.output.lower()


# ---- structured logging -------------------------------------------------


class TestTickLogging:
    """A real tick emits structured per-source log lines."""

    def test_tier_a_logs_per_source_ingest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The Tier-A run logs a `sync.tier_a.ingest source=journal` line."""
        vault, src = _vault_with_journal(tmp_path, entries=1)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: _fake_config(vault, src)
        )
        with caplog.at_level(logging.INFO, logger="creek.cli"):
            runner.invoke(app, ["sync", "--tier", "A", "--vault", str(vault)])
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("sync.tier_a.ingest source=journal" in m for m in messages)
        assert any("sync.tier_a.done" in m for m in messages)
