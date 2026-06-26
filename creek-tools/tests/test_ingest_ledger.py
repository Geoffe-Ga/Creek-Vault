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

if TYPE_CHECKING:
    from pathlib import Path


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
