"""Soft-tomb + restore for vanished mutable source units (#674).

On a *full-source* markdown ingest pass, a ledgered ``source_key`` that is no
longer present is soft-tombed: its fragment moves to ``10-Liminal/Orphaned/``
and is marked ``lifecycle: orphaned``. A re-run is idempotent (no re-tomb), and
a re-added source unit restores the live fragment under its preserved id. A
single-file ``--input`` run never tombs (guards the incremental epic).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek.cli import _run_ingest
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.ledger import SourceLedger
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a minimal vault for ingest + tomb/restore."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "10-Liminal/Orphaned",
        "personal/journal",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _ingest(vault: Path, target: Path) -> tuple[int, list[str], int]:
    """Run the markdown ingestor over *target* (a dir = full-source pass)."""
    return _run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=target,
        vault_path=vault,
    )


def _journal_files(vault: Path) -> list[Path]:
    """Live fragment files under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _orphan_files(vault: Path) -> list[Path]:
    """Tombed fragment files under ``10-Liminal/Orphaned``."""
    return sorted((vault / "10-Liminal" / "Orphaned").rglob("*.md"))


def _journal_fragment(platform: SourcePlatform = SourcePlatform.JOURNAL) -> Fragment:
    """Build a native journal fragment with a fixed id."""
    return Fragment(
        id="frag-fixed01", title="Day", source=FragmentSource(platform=platform)
    )


# ---- Ledger tombed flag -------------------------------------------------


class TestLedgerTombed:
    """The ledger tracks tombed state and excludes it from live keys."""

    def test_tombed_roundtrips_and_live_keys_excludes(self, tmp_path: Path) -> None:
        """A tombed record reloads tombed and is absent from live_keys()."""
        ledger = SourceLedger.load(tmp_path, source="markdown")
        ledger.record("a.md", "frag-a", "h1", tombed=False)
        ledger.record("b.md", "frag-b", "h2", tombed=True)

        reloaded = SourceLedger.load(tmp_path, source="markdown")
        rec_b = reloaded.get("b.md")
        assert rec_b is not None
        assert rec_b.tombed is True
        assert reloaded.live_keys() == {"a.md"}


# ---- Writer tomb / restore (unit) --------------------------------------


class TestTombAndRestore:
    """`tomb_fragment` moves+marks; `restore_fragment` moves back+clears."""

    def test_tomb_moves_and_marks(self, tmp_path: Path) -> None:
        """Tombing relocates the fragment and stamps the orphan marker."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        writer.write_fragment(_journal_fragment(), body="body")

        tombed = writer.tomb_fragment("frag-fixed01")

        assert tombed is not None
        assert tombed.parent == vault / "10-Liminal" / "Orphaned"
        assert _journal_files(vault) == []  # moved out of 01-Fragments
        post = frontmatter.load(str(tombed))
        assert post["id"] == "frag-fixed01"
        assert post["lifecycle"] == "orphaned"
        assert post["orphaned_at"]

    def test_tomb_missing_returns_none(self, tmp_path: Path) -> None:
        """Tombing an unmapped id is a no-op returning None."""
        vault = _make_vault(tmp_path)
        assert VaultWriter(vault_path=vault).tomb_fragment("frag-nope") is None

    def test_restore_moves_back_and_clears_marker(self, tmp_path: Path) -> None:
        """Restoring relocates back and removes the orphan marker."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        frag = _journal_fragment()
        writer.write_fragment(frag, body="body")
        writer.tomb_fragment("frag-fixed01")

        restored = writer.restore_fragment(frag)

        assert restored is not None
        assert restored.parent == vault / "01-Fragments" / "Journal"
        assert _orphan_files(vault) == []
        post = frontmatter.load(str(restored))
        assert "lifecycle" not in post.metadata
        assert "orphaned_at" not in post.metadata

    def test_restore_missing_returns_none(self, tmp_path: Path) -> None:
        """Restoring with no tombed file returns None."""
        vault = _make_vault(tmp_path)
        assert (
            VaultWriter(vault_path=vault).restore_fragment(_journal_fragment()) is None
        )


# ---- End-to-end: delete -> tomb -> restore -----------------------------


class TestOrphanLifecycle:
    """A deleted journal entry is tombed, idempotently, then restored."""

    def test_deleted_then_restored(self, tmp_path: Path) -> None:
        """Delete soft-tombs; re-run is a no-op; re-add restores same id."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        entry = journal / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nToday's entry.\n", encoding="utf-8"
        )
        _, errors, _ = _ingest(vault, journal)
        assert errors == []
        frag_id = frontmatter.load(str(_journal_files(vault)[0]))["id"]

        # Delete the source unit, re-run the full-source pass.
        entry.unlink()
        _, errors, _ = _ingest(vault, journal)
        assert errors == []
        assert _journal_files(vault) == []  # no longer live
        orphans = _orphan_files(vault)
        assert len(orphans) == 1
        tombed = frontmatter.load(str(orphans[0]))
        assert tombed["id"] == frag_id
        assert tombed["lifecycle"] == "orphaned"

        # Idempotent: a second run neither errors nor re-tombs.
        _, errors, _ = _ingest(vault, journal)
        assert errors == []
        assert len(_orphan_files(vault)) == 1

        # Re-add the source unit -> live fragment restored with same id.
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nToday's entry, back again.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, journal)
        assert errors == []
        live = _journal_files(vault)
        assert len(live) == 1
        post = frontmatter.load(str(live[0]))
        assert post["id"] == frag_id
        assert "back again" in post.content
        assert _orphan_files(vault) == []

    def test_single_file_input_does_not_tomb(self, tmp_path: Path) -> None:
        """A single-file --input run must not tomb other units (incremental guard)."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        a = journal / "2026-06-01.md"
        b = journal / "2026-06-02.md"
        a.write_text("---\ndate: 2026-06-01\n---\nEntry A.\n", encoding="utf-8")
        b.write_text("---\ndate: 2026-06-02\n---\nEntry B.\n", encoding="utf-8")
        _ingest(vault, journal)  # full-source pass: both live
        assert len(_journal_files(vault)) == 2

        # Ingest ONLY a as a single file: b is absent from this run's seen set,
        # but a single-file input must never compute a gone set.
        _ingest(vault, a)

        assert len(_journal_files(vault)) == 2  # b untouched
        assert _orphan_files(vault) == []
