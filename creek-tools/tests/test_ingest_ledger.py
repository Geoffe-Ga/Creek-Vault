"""Tests for the per-source ingest ledger + ``source_key`` skeleton (#672).

The ledger maps a stable ``source_key`` (the vault-relative path of a mutable
source unit) to the fragment it produced, the content hash at last ingest, and
a timestamp. This issue is a **no-op pass-through**: it records entries and
populates ``source.origin_key`` but changes no write/skip behaviour — a changed
journal entry still mints a new id here (update-in-place is #673).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import frontmatter

from creek.cli import _run_ingest
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.ledger import LedgerRecord, SourceLedger
from creek.ingest.pipeline import run_ingest
from creek.ingest.spreadsheets import SpreadsheetIngestor

if TYPE_CHECKING:
    from pathlib import Path

    from creek.ingest.pipeline import IngestRunResult


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a minimal vault the writer + ledger can target."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "personal/journal",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _ingest_markdown(vault: Path, entry: Path) -> tuple[int, list[str], int]:
    """Run the markdown ingestor through the real ``_run_ingest`` seam."""
    return _run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=entry,
        vault_path=vault,
    )


def _written_fragments(vault: Path) -> list[frontmatter.Post]:
    """Load every fragment written under ``01-Fragments``."""
    return [
        frontmatter.load(str(p)) for p in sorted((vault / "01-Fragments").rglob("*.md"))
    ]


# ---- SourceLedger (unit) -----------------------------------------------


class TestSourceLedger:
    """The ledger round-trips records and resolves last-write-wins."""

    def test_path_for_lands_under_meta_state(self, tmp_path: Path) -> None:
        """The ledger file lives under 00-Creek-Meta/State/ingest/."""
        path = SourceLedger.path_for(tmp_path, "markdown")
        assert (
            path == tmp_path / "00-Creek-Meta" / "State" / "ingest" / "markdown.jsonl"
        )

    def test_content_hash_is_deterministic(self) -> None:
        """content_hash is a stable SHA-256 of the content."""
        assert SourceLedger.content_hash("abc") == hashlib.sha256(b"abc").hexdigest()
        assert SourceLedger.content_hash("abc") != SourceLedger.content_hash("abd")

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        """Loading a ledger with no backing file yields no records."""
        ledger = SourceLedger.load(tmp_path, source="markdown")
        assert ledger.get("anything") is None
        assert len(ledger) == 0

    def test_record_then_reload_roundtrips(self, tmp_path: Path) -> None:
        """A recorded entry survives a reload from disk."""
        ledger = SourceLedger.load(tmp_path, source="markdown")
        ledger.record(
            "a/b.md", "frag-aaa", "hash1", last_seen="2026-06-26T00:00:00+00:00"
        )

        reloaded = SourceLedger.load(tmp_path, source="markdown")
        rec = reloaded.get("a/b.md")
        assert rec == LedgerRecord(
            source_key="a/b.md",
            fragment_id="frag-aaa",
            content_hash="hash1",
            last_seen="2026-06-26T00:00:00+00:00",
        )

    def test_record_upsert_last_write_wins(self, tmp_path: Path) -> None:
        """Re-recording the same source_key resolves to the latest entry."""
        ledger = SourceLedger.load(tmp_path, source="markdown")
        ledger.record("a/b.md", "frag-aaa", "hash1")
        ledger.record("a/b.md", "frag-aaa", "hash2")

        reloaded = SourceLedger.load(tmp_path, source="markdown")
        rec = reloaded.get("a/b.md")
        assert rec is not None
        assert rec.content_hash == "hash2"

    def test_record_auto_stamps_last_seen(self, tmp_path: Path) -> None:
        """Omitting last_seen stamps an ISO-8601 timestamp."""
        ledger = SourceLedger.load(tmp_path, source="markdown")
        rec = ledger.record("a/b.md", "frag-aaa", "hash1")
        assert rec.last_seen  # non-empty ISO timestamp
        assert "T" in rec.last_seen

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        """A corrupt JSONL line does not abort loading the rest."""
        path = SourceLedger.path_for(tmp_path, "markdown")
        path.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {
                "source_key": "a/b.md",
                "fragment_id": "frag-aaa",
                "content_hash": "h",
                "last_seen": "t",
            }
        )
        path.write_text(f"{{not json\n\n{good}\n", encoding="utf-8")
        ledger = SourceLedger.load(tmp_path, source="markdown")
        assert ledger.get("a/b.md") is not None

    def test_records_missing_required_fields_are_rejected(self, tmp_path: Path) -> None:
        """A row missing a required field (e.g. content_hash) is not loaded."""
        path = SourceLedger.path_for(tmp_path, "markdown")
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = json.dumps(
            {"source_key": "a/b.md", "fragment_id": "frag-aaa", "last_seen": "t"}
        )
        empty_hash = json.dumps(
            {
                "source_key": "c/d.md",
                "fragment_id": "frag-bbb",
                "content_hash": "",
                "last_seen": "t",
            }
        )
        path.write_text(f"{partial}\n{empty_hash}\n", encoding="utf-8")
        ledger = SourceLedger.load(tmp_path, source="markdown")
        assert ledger.get("a/b.md") is None  # missing content_hash
        assert ledger.get("c/d.md") is None  # empty content_hash
        assert len(ledger) == 0


# ---- Markdown ingest wiring (no-op pass-through) -----------------------


class TestMarkdownLedgerWiring:
    """Markdown ingest populates origin_key + the ledger, behaviour unchanged."""

    def test_origin_key_and_ledger_recorded(self, tmp_path: Path) -> None:
        """An ingested journal entry carries origin_key and a ledger record."""
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nFirst draft of today.\n",
            encoding="utf-8",
        )

        written, errors, _ = _ingest_markdown(vault, entry)
        assert written == 1, errors

        frags = _written_fragments(vault)
        assert len(frags) == 1
        post = frags[0]
        assert post["source"]["origin_key"] == "personal/journal/2026-06-26.md"

        ledger = SourceLedger.load(vault, source="markdown")
        rec = ledger.get("personal/journal/2026-06-26.md")
        assert rec is not None
        assert rec.fragment_id == post["id"]
        assert rec.content_hash  # non-empty
        assert rec.last_seen

    def test_reingest_unchanged_is_still_a_noop(self, tmp_path: Path) -> None:
        """Re-ingesting unchanged input writes no duplicate (skeleton invariant)."""
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nStable content.\n",
            encoding="utf-8",
        )

        _ingest_markdown(vault, entry)
        _ingest_markdown(vault, entry)

        assert len(_written_fragments(vault)) == 1

    def test_non_markdown_source_skips_ledger(self, tmp_path: Path) -> None:
        """Only the markdown source writes a ledger in this skeleton."""
        vault = _make_vault(tmp_path)
        # No markdown ingest run -> no ledger file should exist.
        assert not SourceLedger.path_for(vault, "discord").exists()


# ---- Per-sheet ledger identity for uploaded workbooks (#1305) ----------


_UPLOAD_RELDIR = "00-Creek-Meta/adepthood/uploads"
"""Vault-relative staging directory the upload path writes into."""

_EXPECTED_SHEET_KEYS = {
    f"{_UPLOAD_RELDIR}/book.xlsx#Budget",
    f"{_UPLOAD_RELDIR}/book.xlsx#Notes",
    f"{_UPLOAD_RELDIR}/book.xlsx#Q3",
}
"""The three per-sheet ``source_key`` values one staged workbook must produce."""


def _stage_multi_sheet_workbook(vault: Path) -> Path:
    """Write a real 3-sheet workbook into the upload staging directory.

    Each sheet carries a header row and a data row so none of them is
    dropped by the ``sheet.is_empty`` filter in ``SpreadsheetIngestor.parse``.
    """
    import openpyxl

    staged = vault / _UPLOAD_RELDIR / "book.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    first = workbook.active
    assert first is not None
    first.title = "Budget"
    workbook.create_sheet(title="Notes")
    workbook.create_sheet(title="Q3")
    for index, name in enumerate(("Budget", "Notes", "Q3")):
        worksheet = workbook[name]
        worksheet.append(["item", "amount"])
        worksheet.append([f"row-{index}", str(index * 10)])
    workbook.save(staged)
    return staged


def _ingest_staged_workbook(vault: Path, staged: Path) -> IngestRunResult:
    """Run the spreadsheet ingestor over the staged upload, ledger-backed."""
    return run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=staged,
        vault_path=vault,
        ledger_source="upload",
    )


def _fragment_paths(vault: Path) -> list[Path]:
    """Return every fragment markdown file under ``01-Fragments``, sorted."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


class TestUploadedWorkbookLedgerIdentity:
    """A staged multi-sheet workbook is one ledgered unit per sheet (#1305)."""

    def test_ledgered_multi_sheet_workbook_is_created_then_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """Three sheets create three fragments, then re-ingest as three unchanged.

        Measured at HEAD, this run reports::

            run1  created=1 updated=0 unchanged=2  files=1
            run2  created=0 updated=0 unchanged=3  files=1

        and the one surviving file holds the FIRST sheet (``Budget``) —
        ``VaultWriter._write_model``'s duplicate-id early return
        returns the already-written path for a duplicate id, so the first
        writer wins and sheets 2..N are dropped. Any assertion phrased
        around "the last sheet survives" would be red before *and* after
        the fix and would certify nothing.

        A per-sheet *fragment id* alone does not fix this run, which is
        why the ledgered path needs its own test. ``derive_source_key``
        keys on the file, so all three sheets resolve to one
        ``origin_key``, one ``LedgerRecord``, and therefore one identity:
        ``write_fragment_idempotent`` assigns ``fragment.id =
        record.fragment_id`` on every branch of ``write_fragment_idempotent``
        and the freshly-derived per-sheet ids are overwritten before the
        write. The ledger key has to become per-sheet too.

        ``updated`` must stay at zero on both runs. Nothing was edited
        between them, so an ``updated`` count here is three sheets
        overwriting each other in place — the loss wearing a success
        message.
        """
        vault = _make_vault(tmp_path)
        staged = _stage_multi_sheet_workbook(vault)

        run1 = _ingest_staged_workbook(vault, staged)
        assert run1.errors == [], run1.errors
        assert (run1.created, run1.updated, run1.unchanged) == (3, 0, 0)
        assert len(_fragment_paths(vault)) == 3

        run2 = _ingest_staged_workbook(vault, staged)
        assert run2.errors == [], run2.errors
        assert (run2.created, run2.updated, run2.unchanged) == (0, 0, 3)
        assert len(_fragment_paths(vault)) == 3

    def test_ledgered_sheet_origin_keys_are_per_sheet_and_stable(
        self,
        tmp_path: Path,
    ) -> None:
        """Each sheet gets its own ``origin_key``, and it does not move on re-ingest.

        ``source.origin_key`` is the key the RTBF purge sweep matches on,
        so it is not merely an identity nicety: a workbook whose three
        sheets share one key is three fragments the operator can only
        delete as one, and a key that changes between two identical
        ingests is a fragment the purge can no longer find at all.

        At HEAD all three sheets derive the same key
        (``00-Creek-Meta/adepthood/uploads/book.xlsx``), so the ledger
        holds exactly one record and this test is red on the key set.
        """
        vault = _make_vault(tmp_path)
        staged = _stage_multi_sheet_workbook(vault)

        first = _ingest_staged_workbook(vault, staged)
        assert first.errors == [], first.errors

        ledger = SourceLedger.load(vault, source="upload")
        assert len(ledger) == 3
        assert ledger.live_keys() == _EXPECTED_SHEET_KEYS

        posts = _written_fragments(vault)
        assert len(posts) == 3
        assert {post["source"]["origin_key"] for post in posts} == _EXPECTED_SHEET_KEYS
        for post in posts:
            record = ledger.get(post["source"]["origin_key"])
            assert record is not None, post["source"]["origin_key"]
            assert record.fragment_id == post["id"]

        second = _ingest_staged_workbook(vault, staged)
        assert second.errors == [], second.errors
        reloaded = SourceLedger.load(vault, source="upload")
        assert reloaded.live_keys() == _EXPECTED_SHEET_KEYS
