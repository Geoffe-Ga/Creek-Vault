"""Incremental ingest — ``--since`` + ledger-driven ``--incremental`` (#677).

Only source units whose mtime (``--since``) or content-hash (``--incremental``,
ledger-driven) changed are processed; unchanged units are skipped. With neither
flag, every unit is processed (backward-compatible).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import typer

from creek.cli import _parse_since_arg, _run_ingest
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.ledger import LedgerRecord
from creek.pipeline import unit_is_changed

if TYPE_CHECKING:
    from pathlib import Path


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a minimal vault."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "10-Liminal/Orphaned",
        "personal/journal",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _ingest(
    vault: Path,
    target: Path,
    *,
    since: datetime | None = None,
    incremental: bool = False,
    print_summary: bool = False,
) -> tuple[int, list[str], int]:
    """Run the markdown ingestor with optional incremental cursors."""
    return _run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=target,
        vault_path=vault,
        since=since,
        incremental=incremental,
        print_summary=print_summary,
    )


def _two_entries(journal: Path) -> None:
    """Write two journal entries."""
    (journal / "a.md").write_text(
        "---\ndate: 2026-06-01\n---\nAlpha.\n", encoding="utf-8"
    )
    (journal / "b.md").write_text(
        "---\ndate: 2026-06-02\n---\nBeta.\n", encoding="utf-8"
    )


# ---- unit_is_changed (pure) --------------------------------------------


class TestUnitIsChanged:
    """The incremental change predicate across both cursor modes."""

    def test_since_newer_is_changed(self) -> None:
        """A unit newer than the cutoff is processed."""
        assert unit_is_changed(
            datetime(2026, 6, 2, tzinfo=UTC),
            "h",
            None,
            datetime(2026, 6, 1, tzinfo=UTC),
        )

    def test_since_older_is_skipped(self) -> None:
        """A unit at/older than the cutoff is skipped."""
        assert not unit_is_changed(
            datetime(2026, 6, 1, tzinfo=UTC),
            "h",
            None,
            datetime(2026, 6, 2, tzinfo=UTC),
        )

    def test_since_naive_timestamp_assumed_utc(self) -> None:
        """A naive timestamp is compared as UTC, not rejected."""
        assert unit_is_changed(
            datetime(2026, 6, 2), "h", None, datetime(2026, 6, 1, tzinfo=UTC)
        )

    def test_ledger_hash_differs_is_changed(self) -> None:
        """Without a cutoff, a differing content hash is a change."""
        rec = LedgerRecord("k", "frag-a", "oldhash", "t")
        assert unit_is_changed(datetime(2026, 6, 1, tzinfo=UTC), "newhash", rec, None)

    def test_ledger_hash_same_is_skipped(self) -> None:
        """Without a cutoff, an unchanged content hash is skipped."""
        rec = LedgerRecord("k", "frag-a", "samehash", "t")
        assert not unit_is_changed(
            datetime(2026, 6, 1, tzinfo=UTC), "samehash", rec, None
        )

    def test_new_unit_no_record_is_changed(self) -> None:
        """A unit with no prior ledger record is always processed."""
        assert unit_is_changed(datetime(2026, 6, 1, tzinfo=UTC), "h", None, None)


# ---- _parse_since_arg ---------------------------------------------------


class TestParseSinceArg:
    """--since accepts an ISO timestamp or a duration."""

    def test_iso_timestamp(self) -> None:
        """An ISO timestamp parses to an aware UTC datetime."""
        assert _parse_since_arg("2026-06-25T00:00:00") == datetime(
            2026, 6, 25, tzinfo=UTC
        )

    def test_duration_falls_back_to_parse_since(self) -> None:
        """A duration string parses to a datetime in the past."""
        result = _parse_since_arg("7d")
        assert isinstance(result, datetime)
        assert result < datetime.now(tz=UTC)

    def test_invalid_exits(self) -> None:
        """An unparseable value exits non-zero."""
        with pytest.raises(typer.Exit):
            _parse_since_arg("not-a-date!!")


# ---- End-to-end incremental --------------------------------------------


class TestIncrementalIngest:
    """Through the ingest seam, only changed units are processed."""

    def test_incremental_skips_unchanged_by_hash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Editing one of two entries processes only it; the other is skipped."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        _two_entries(journal)
        _ingest(vault, journal)  # populate the ledger
        capsys.readouterr()

        # Edit only a.md.
        (journal / "a.md").write_text(
            "---\ndate: 2026-06-01\n---\nAlpha revised.\n", encoding="utf-8"
        )
        _ingest(vault, journal, incremental=True, print_summary=True)
        out = capsys.readouterr().out
        assert "1 updated" in out  # a.md changed
        assert "1 skipped" in out  # b.md unchanged by hash

    def test_since_future_skips_all(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A far-future cutoff skips every unit regardless of timestamp source."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        _two_entries(journal)
        _ingest(
            vault, journal, since=datetime(2099, 1, 1, tzinfo=UTC), print_summary=True
        )
        out = capsys.readouterr().out
        assert "0 created" in out
        assert "2 skipped" in out

    def test_since_overrides_ledger_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A past --since cutoff processes unchanged units (no hash-skip)."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        _two_entries(journal)
        _ingest(vault, journal)  # populate ledger
        capsys.readouterr()

        # Re-ingest UNCHANGED with a past cutoff: the since-path processes both
        # (skipped=0), where the ledger-default would have hash-skipped them.
        _ingest(
            vault,
            journal,
            since=datetime(2000, 1, 1, tzinfo=UTC),
            print_summary=True,
        )
        assert "0 skipped" in capsys.readouterr().out

    def test_no_flags_processes_all(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without incremental flags, nothing is skipped (backward-compatible)."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        _two_entries(journal)
        _ingest(vault, journal)  # populate ledger
        capsys.readouterr()

        _ingest(vault, journal, print_summary=True)  # no flags
        assert "0 skipped" in capsys.readouterr().out
