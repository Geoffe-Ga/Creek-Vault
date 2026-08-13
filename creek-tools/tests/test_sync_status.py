"""`creek sync --status` + per-tick logging + per-source state (#680).

A real Tier-A run records per-source ingested counts in ``last-run.json``
(keeping the #676 top-level keys), `--status` renders them, and each tick emits
structured stdlib log lines.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import SourceRunRecord, _sync_state_path, _write_sync_state, app
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
        _write_sync_state(
            vault,
            "A",
            dry_run=False,
            source_records={"journal": SourceRunRecord(ingested=3)},
        )
        state = json.loads(_sync_state_path(vault).read_text(encoding="utf-8"))
        assert state["tier"] == "A"
        assert "at" in state
        assert state["dry_run"] is False
        assert state["sources"]["journal"]["ingested"] == 3

    def test_merges_prior_sources(self, tmp_path: Path) -> None:
        """A run that touches one source preserves the others' last entry."""
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_sync_state(
            vault,
            "A",
            dry_run=False,
            source_records={"journal": SourceRunRecord(ingested=2)},
        )
        _write_sync_state(
            vault,
            "A",
            dry_run=False,
            source_records={"gdrive": SourceRunRecord(ingested=0)},
        )
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
        # The status table renders the journal row (the count itself is asserted
        # against the state file above — `"2" in output` would match dates too).
        assert "journal" in result.output
        assert "ingested" in result.output  # the table header rendered

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

    def test_status_without_vault_uses_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --vault, --status falls back to the config's vault path."""
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig(vault_path=vault)
        )
        result = runner.invoke(app, ["sync", "--status"])  # no --vault
        assert result.exit_code == 0, result.output
        assert "no sync has run" in result.output.lower()

    def test_status_marks_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run last recorded shows (dry-run) in the status header."""
        vault, src = _vault_with_journal(tmp_path, entries=1)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: _fake_config(vault, src)
        )
        runner.invoke(app, ["sync", "--tier", "A", "--dry-run", "--vault", str(vault)])
        result = runner.invoke(app, ["sync", "--status", "--vault", str(vault)])
        assert "(dry-run)" in result.output


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


# ---- failures are visible, and legacy state still renders (#1372) -------


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# The Unicode Box Drawing block — the glyphs Rich frames the status table in.
_BOX_DRAWING_RE = re.compile("[─-╿]")


def _flat(text: str) -> str:
    """Return *text* with ANSI codes, Rich table borders and wrapping removed.

    ``--status`` renders a Rich table, which frames every cell in box-drawing
    glyphs and soft-wraps long ones. A failure line that reaches the operator
    split across two rows has still reached them, so the assertions below are
    about presence and attribution, not about layout.

    Args:
        text: Raw captured CLI output.

    Returns:
        The single-spaced, escape-free, border-free form of *text*.
    """
    return " ".join(_BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", text)).split())


_LEGACY_AT = "2026-01-01T00:00:00+00:00"


def _write_legacy_state(vault: Path, sources: dict[str, object]) -> Path:
    """Write a ``last-run.json`` in the pre-#1372 shape, byte for byte.

    Hand-written rather than produced by ``_write_sync_state`` on purpose: the
    point of the backward-compatibility tests is a file that is already on an
    operator's disk, written by a version of this code that no longer exists.
    Generating it with today's writer would only prove today's writer agrees
    with itself.

    Args:
        vault: Vault root to write the state file under.
        sources: The per-source block, exactly as it should land on disk.

    Returns:
        The path written.
    """
    path = _sync_state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"tier": "A", "at": _LEGACY_AT, "dry_run": False, "sources": sources}
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


class TestFailureVisibility:
    """``--status`` shows what the last tick failed to fetch or parse (#1372).

    The table's only per-source number was ``ingested``, and an ``ingested``
    of zero is what a healthy quiet tick and a totally failed one both look
    like. These tests pin the new column and the printed lines, and — just as
    importantly — pin that a state file written before either existed still
    renders instead of throwing.
    """

    def test_status_renders_a_failed_column(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The table gains a failed count, and the recorded lines are printed.

        The count alone would tell the operator only that something is wrong;
        the line is what names the file they now have to go and fetch by hand.
        """
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_sync_state(
            vault,
            "A",
            dry_run=False,
            source_records={
                "gdrive": SourceRunRecord(
                    ingested=0, failures=("pull: a.docx: RuntimeError: 403",)
                ),
            },
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: CreekConfig(vault_path=vault),
        )

        result = runner.invoke(app, ["sync", "--status", "--vault", str(vault)])

        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        assert "failed" in flat.lower(), flat
        assert "a.docx" in flat, flat

    def test_a_legacy_state_file_without_the_new_keys_still_renders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-#1372 ``last-run.json`` renders rather than raising.

        Every existing vault has one of these on disk, with no ``failures``
        and no ``failure_count`` key. ``--status`` is the command an operator
        reaches for when they suspect something is wrong, so it is the worst
        possible place for a ``KeyError`` on the first run after an upgrade.
        """
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_legacy_state(
            vault,
            {"journal": {"tier": "A", "at": _LEGACY_AT, "ingested": 3}},
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: CreekConfig(vault_path=vault),
        )

        result = runner.invoke(app, ["sync", "--status", "--vault", str(vault)])

        assert result.exit_code == 0, result.output
        assert "journal" in _flat(result.output), result.output

    def test_a_legacy_entry_survives_a_new_run(self, tmp_path: Path) -> None:
        """A source untouched by the new run keeps its legacy entry intact.

        ``_write_sync_state`` merges rather than replaces, and the merge now
        has to carry entries in two different shapes at once: the source this
        tick touched gains ``failures``/``failure_count``, and the one it did
        not keeps exactly the keys it already had. A writer that normalised
        every entry to the new shape would be rewriting history it never
        observed.
        """
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_legacy_state(
            vault,
            {"journal": {"tier": "A", "at": _LEGACY_AT, "ingested": 3}},
        )

        _write_sync_state(
            vault,
            "A",
            dry_run=False,
            source_records={"gdrive": SourceRunRecord(ingested=1)},
        )

        sources = json.loads(_sync_state_path(vault).read_text())["sources"]
        assert sources["journal"]["ingested"] == 3, sources
        assert sources["journal"]["at"] == _LEGACY_AT, sources
        assert sources["gdrive"]["ingested"] == 1, sources

    def test_a_corrupt_failures_value_does_not_crash_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wrongly-typed failure keys degrade the row, they do not kill the table.

        The existing reader already tolerates a non-dict ``sources`` and a
        non-dict entry (cli.py:868, cli.py:898) because this file is plain
        JSON in a folder the operator can open and edit. The two new keys get
        the same treatment: a hand-mangled ``"failures": "oops"`` must not be
        the thing that stops them seeing the rest of the table.
        """
        vault = tmp_path / "v"
        (vault / "00-Creek-Meta").mkdir(parents=True)
        _write_legacy_state(
            vault,
            {
                "gdrive": {
                    "tier": "A",
                    "at": _LEGACY_AT,
                    "ingested": 0,
                    "failures": "oops",
                    "failure_count": "nope",
                },
            },
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: CreekConfig(vault_path=vault),
        )

        result = runner.invoke(app, ["sync", "--status", "--vault", str(vault)])

        assert result.exit_code == 0, result.output
        assert "gdrive" in _flat(result.output), result.output
