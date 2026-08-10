"""Update-in-place for edited mutable source units (#673).

When a markdown (journal) source unit with an existing ``source_key`` is
re-ingested with *changed* content, the existing fragment is rewritten in
place — same fragment id, classifications and links preserved — instead of a
new fragment being minted and the old one orphaned (SPEC R1, decision #1).
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
    """Scaffold a minimal vault for ingest + update."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "personal/journal",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _ingest(vault: Path, entry: Path) -> tuple[int, list[str], int]:
    """Run the markdown ingestor through the real ``_run_ingest`` seam."""
    return _run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=entry,
        vault_path=vault,
    )


def _journal_files(vault: Path) -> list[Path]:
    """Every fragment file written under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _set_frontmatter(path: Path, **fields: object) -> None:
    """Overlay extra frontmatter keys onto an existing fragment file."""
    post = frontmatter.load(str(path))
    for key, value in fields.items():
        post[key] = value
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


# ---- VaultWriter.update_fragment (unit) --------------------------------


class TestUpdateFragment:
    """In-place body rewrite preserves frontmatter; missing file -> None."""

    def _seed(self, vault: Path) -> Fragment:
        """Write one native journal fragment and return its model."""
        frag = Fragment(
            id="frag-fixed001",
            title="Day",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        VaultWriter(vault_path=vault).write_fragment(frag, body="original body")
        return frag

    def test_update_rewrites_body_preserving_frontmatter(self, tmp_path: Path) -> None:
        """The body changes; id and on-disk classification survive."""
        vault = _make_vault(tmp_path)
        frag = self._seed(vault)
        path = _journal_files(vault)[0]
        _set_frontmatter(path, classification_method="llm", tags=["insight"])

        writer = VaultWriter(vault_path=vault)
        updated = writer.update_fragment(frag, body="revised body")

        assert updated == path  # same file, in place
        post = frontmatter.load(str(path))
        assert "revised body" in post.content
        assert "original body" not in post.content
        assert post["id"] == "frag-fixed001"
        assert post["classification_method"] == "llm"
        assert post["tags"] == ["insight"]

    def test_update_returns_none_when_no_existing_file(self, tmp_path: Path) -> None:
        """Updating an id with no mapped file returns None (caller falls back)."""
        vault = _make_vault(tmp_path)
        ghost = Fragment(
            id="frag-missing99",
            title="Ghost",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        writer = VaultWriter(vault_path=vault)
        assert writer.update_fragment(ghost, body="x") is None

    def test_target_dir_routes_native_and_borrowed(self, tmp_path: Path) -> None:
        """Routing is the single source of truth for native + borrowed authors."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        native = Fragment(
            id="frag-n",
            title="N",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        borrowed = Fragment(
            id="frag-b",
            title="B",
            source=FragmentSource(platform=SourcePlatform.JOURNAL, author_slug="ada"),
        )
        assert writer._fragment_target_dir(native) == (
            vault / "01-Fragments" / "Journal"
        )
        assert writer._fragment_target_dir(borrowed) == (
            vault / "11-Other-Authors" / "ada"
        )


# ---- End-to-end: ledger-driven changed branch --------------------------


class TestEditedJournalUpdatesInPlace:
    """An edited journal entry updates the same fragment, not a duplicate."""

    def test_edit_updates_same_fragment_id(self, tmp_path: Path) -> None:
        """Editing a journal entry preserves id + classifications, no orphan."""
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nFirst draft of today.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, entry)
        assert errors == []

        files = _journal_files(vault)
        assert len(files) == 1
        original_id = frontmatter.load(str(files[0]))["id"]
        # Simulate prior classification work that must survive the edit.
        _set_frontmatter(files[0], classification_method="llm", tags=["insight"])

        # Edit the SAME source unit (same source_key), new content.
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nSecond, revised draft of today.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, entry)
        assert errors == []

        files_after = _journal_files(vault)
        assert len(files_after) == 1  # no duplicate, no orphan
        post = frontmatter.load(str(files_after[0]))
        assert post["id"] == original_id  # id preserved
        assert "revised draft" in post.content  # body updated
        assert post["classification_method"] == "llm"  # OPS-001 preserved
        assert post["tags"] == ["insight"]  # tags preserved

        ledger = SourceLedger.load(vault, source="markdown")
        rec = ledger.get("personal/journal/2026-06-26.md")
        assert rec is not None
        assert rec.fragment_id == original_id  # ledger still maps to same id

    def test_unchanged_reingest_remains_noop(self, tmp_path: Path) -> None:
        """Re-ingesting identical content writes no duplicate and no rewrite."""
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nStable content.\n",
            encoding="utf-8",
        )
        _ingest(vault, entry)
        first = frontmatter.load(str(_journal_files(vault)[0]))["id"]
        _ingest(vault, entry)

        files = _journal_files(vault)
        assert len(files) == 1
        assert frontmatter.load(str(files[0]))["id"] == first

    def test_edit_after_out_of_band_delete_recreates_with_preserved_id(
        self, tmp_path: Path
    ) -> None:
        """If the vault fragment vanished, an edit re-creates it with the same id.

        Exercises the ``update_fragment → None`` fallback: the ledger still maps
        the source unit to its id, the file is gone, and the changed-branch must
        fall back to a fresh write under the *preserved* id (not a new one).
        """
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nFirst draft.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, entry)
        assert errors == []
        original_id = frontmatter.load(str(_journal_files(vault)[0]))["id"]

        # The vault fragment disappears out of band (the ledger still knows it).
        _journal_files(vault)[0].unlink()
        assert _journal_files(vault) == []

        # Edit the source and re-ingest -> fallback write under the preserved id.
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nRewritten after loss.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, entry)
        assert errors == []

        files = _journal_files(vault)
        assert len(files) == 1
        post = frontmatter.load(str(files[0]))
        assert post["id"] == original_id  # preserved id, not a new hash
        assert "Rewritten after loss" in post.content

        ledger = SourceLedger.load(vault, source="markdown")
        rec = ledger.get("personal/journal/2026-06-26.md")
        assert rec is not None
        assert rec.fragment_id == original_id

    def test_stale_index_edit_is_not_lost(self, tmp_path: Path) -> None:
        """A poisoned id index must not swallow the edit into a foreign fragment.

        The ledger still maps the source unit to its fragment id, but the
        directory's ``.id-index.jsonl`` now maps that id onto a *bystander*
        file. ``update_fragment`` must decline the foreign file, so the changed
        branch falls through to its ``updated is None`` fallback and re-creates
        the fragment under the preserved id — the edit survives, and the
        bystander is untouched (#1083).
        """
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(
            "---\ndate: 2026-06-26\n---\nFirst draft.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, entry)
        assert errors == []
        original = _journal_files(vault)[0]
        original_id = str(frontmatter.load(str(original))["id"])

        # The real fragment vanishes out of band, a bystander is written into
        # the same directory, and the index is poisoned to name it.
        original.unlink()
        seed = VaultWriter(vault_path=vault)
        bystander = Fragment(
            id="frag-bystander1",
            title="Bystander",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        bystander_path = seed.write_fragment(bystander, body="bystander body")
        # Setup guard: the poison only bites if both share one directory index.
        assert bystander_path.parent == original.parent
        before = bystander_path.read_bytes()
        seed._append_index_entry(
            bystander_path.parent,
            original_id,
            bystander_path.name,
        )

        entry.write_text(
            "---\ndate: 2026-06-26\n---\nRewritten after the index went stale.\n",
            encoding="utf-8",
        )
        _, errors, _ = _ingest(vault, entry)
        assert errors == []

        recreated = [
            path
            for path in _journal_files(vault)
            if frontmatter.load(str(path))["id"] == original_id
        ]
        assert len(recreated) == 1
        post = frontmatter.load(str(recreated[0]))
        assert "Rewritten after the index went stale" in post.content
        assert bystander_path.read_bytes() == before
