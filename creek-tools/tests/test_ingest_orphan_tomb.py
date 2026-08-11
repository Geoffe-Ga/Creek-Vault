"""Soft-tomb + restore for vanished mutable source units (#674).

On a *full-source* markdown ingest pass, a ledgered ``source_key`` that is no
longer present is soft-tombed: its fragment moves to ``10-Liminal/Orphaned/``
and is marked ``lifecycle: orphaned``. A re-run is idempotent (no re-tomb), and
a re-added source unit restores the live fragment under its preserved id. A
single-file ``--input`` run never tombs (guards the incremental epic).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

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


# A fragment's filename stem is ``{date}-{sanitised title}`` (``_compute_base_name``),
# which is unique only *within* one ``01-Fragments/<platform>/`` subfolder. Fixing the
# date and the title makes two fragments in different subfolders land on the same
# stem, which is what collides in the flat ``10-Liminal/Orphaned/`` sink (#1302).
_COLLIDING_CREATED = datetime(2026, 8, 9, tzinfo=UTC)
_COLLIDING_STEM = "2026-08-09-Notes.md"
_MARKER_A = "IRREPLACEABLE-MARKER-ALPHA-7f21"
_MARKER_B = "IRREPLACEABLE-MARKER-BRAVO-3c94"


def _colliding_fragment(frag_id: str, platform: SourcePlatform) -> Fragment:
    """Build a fragment pinned to the shared ``_COLLIDING_STEM`` filename."""
    return Fragment(
        id=frag_id,
        title="Notes",
        source=FragmentSource(platform=platform),
        created=_COLLIDING_CREATED,
    )


def _raw_id_index(target_dir: Path) -> dict[str, str]:
    """Parse ``.id-index.jsonl`` by hand, later-wins, with no repair pass.

    Deliberately bypasses ``VaultWriter._load_index_locked`` and
    ``_find_existing``: both re-resolve a mis-mapped entry by directory scan
    and persist the correction (#1083), which is exactly the healing this
    reader must not perform. Records are self-delimiting since #1120, so the
    file carries blank separator lines; they are skipped.
    """
    index_path = target_dir / ".id-index.jsonl"
    mapping: dict[str, str] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        mapping[str(record["id"])] = str(record["filename"])
    return mapping


def _provenance_paths(vault: Path) -> dict[str, str]:
    """Return each id's most recent recorded provenance ``path``."""
    log_path = vault / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
    latest: dict[str, str] = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        latest[str(record["id"])] = str(record["path"])
    return latest


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


# ---- Writer tomb / restore: filename collisions (#1302) ----------------


class TestTombRestoreFilenameCollisions:
    """Neither tomb nor restore may destroy a same-named bystander file.

    Both directions used to compose their destination as
    ``<dir> / existing.name`` and write it with ``_atomic_write_text`` — an
    ``os.replace`` that overwrites unconditionally. Because a filename stem is
    unique only within one ``01-Fragments/<platform>/`` subfolder, while
    ``10-Liminal/Orphaned/`` is a single flat sink for all of them, a second
    tomb of a same-stemmed fragment silently replaced the first tombstone, and
    a restore lost the same way to a newer fragment holding the freed stem.
    Allocation now runs through ``_atomic_create`` (``O_CREAT | O_EXCL`` plus a
    counter suffix), and these tests keep it that way (#1302).
    """

    def test_tomb_preserves_a_same_named_bystander(self, tmp_path: Path) -> None:
        """A second tomb sharing a filename stem must not clobber the first."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        # Journal -> 01-Fragments/Journal, Discord -> 01-Fragments/Messages.
        # ``_write_model`` mkdirs the Messages folder that ``_make_vault`` omits.
        frag_a = _colliding_fragment("frag-coll-a", SourcePlatform.JOURNAL)
        frag_b = _colliding_fragment("frag-coll-b", SourcePlatform.DISCORD)
        path_a = writer.write_fragment(frag_a, body=_MARKER_A)
        path_b = writer.write_fragment(frag_b, body=_MARKER_B)
        assert path_a != path_b
        assert path_a.name == _COLLIDING_STEM
        assert path_b.name == _COLLIDING_STEM

        tombed_a = writer.tomb_fragment("frag-coll-a")
        tombed_b = writer.tomb_fragment("frag-coll-b")

        assert tombed_a is not None
        assert tombed_b is not None
        bodies = [p.read_text(encoding="utf-8") for p in _orphan_files(vault)]
        # A's body has no other copy in the vault: the live file was unlinked by
        # its own tomb, so if B's tomb replaced the tombstone A is simply gone.
        assert any(_MARKER_A in text for text in bodies)
        assert any(_MARKER_B in text for text in bodies)
        assert len(_orphan_files(vault)) == 2
        assert tombed_a != tombed_b
        assert tombed_a.exists()
        assert tombed_b.exists()
        assert frontmatter.load(str(tombed_a))["id"] == "frag-coll-a"
        assert frontmatter.load(str(tombed_b))["id"] == "frag-coll-b"

    def test_restore_preserves_a_same_named_bystander(self, tmp_path: Path) -> None:
        """A restore must not clobber a newer fragment holding the freed stem."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        frag_c = _colliding_fragment("frag-coll-c", SourcePlatform.JOURNAL)
        writer.write_fragment(frag_c, body=_MARKER_A)
        assert writer.tomb_fragment("frag-coll-c") is not None

        # D takes the stem C freed inside 01-Fragments/Journal.
        frag_d = _colliding_fragment("frag-coll-d", SourcePlatform.JOURNAL)
        path_d = writer.write_fragment(frag_d, body=_MARKER_B)
        assert path_d.name == _COLLIDING_STEM
        bytes_d = path_d.read_text(encoding="utf-8")
        assert _MARKER_B in bytes_d

        restored = writer.restore_fragment(frag_c)

        assert restored is not None
        # D was never tombed and never rewritten: its bytes must be untouched.
        assert path_d.read_text(encoding="utf-8") == bytes_d
        assert restored != path_d
        assert restored.exists()
        assert path_d.exists()
        assert frontmatter.load(str(path_d))["id"] == "frag-coll-d"
        assert frontmatter.load(str(restored))["id"] == "frag-coll-c"

    def test_colliding_tombs_index_to_distinct_filenames(self, tmp_path: Path) -> None:
        """Each tombed id resolves, from disk, to its own tombstone file."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        writer.write_fragment(
            _colliding_fragment("frag-coll-a", SourcePlatform.JOURNAL),
            body=_MARKER_A,
        )
        writer.write_fragment(
            _colliding_fragment("frag-coll-b", SourcePlatform.DISCORD),
            body=_MARKER_B,
        )
        writer.tomb_fragment("frag-coll-a")
        writer.tomb_fragment("frag-coll-b")

        # A fresh writer reloads ``.id-index.jsonl`` from disk instead of reading
        # the first writer's in-process dict, so this pins what the tomb actually
        # *recorded* — the path the create returned, not a locally composed name.
        fresh = VaultWriter(vault_path=vault)
        orphan_dir = vault / "10-Liminal" / "Orphaned"
        found_a = fresh._find_existing("frag-coll-a", orphan_dir)
        found_b = fresh._find_existing("frag-coll-b", orphan_dir)

        assert found_a is not None
        assert found_b is not None
        assert found_a != found_b
        assert frontmatter.load(str(found_a))["id"] == "frag-coll-a"
        assert frontmatter.load(str(found_b))["id"] == "frag-coll-b"

    def test_colliding_tombs_record_the_created_path_not_the_old_name(
        self, tmp_path: Path
    ) -> None:
        """The index and provenance name the file the create actually made.

        Getting the destination right but recording the pre-move filename
        would trade a silent overwrite for a silent orphan: the bystander
        survives, but the id points at a stranger's file.
        """
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        writer.write_fragment(
            _colliding_fragment("frag-coll-a", SourcePlatform.JOURNAL),
            body=_MARKER_A,
        )
        writer.write_fragment(
            _colliding_fragment("frag-coll-b", SourcePlatform.DISCORD),
            body=_MARKER_B,
        )

        tombed_a = writer.tomb_fragment("frag-coll-a")
        tombed_b = writer.tomb_fragment("frag-coll-b")

        assert tombed_a is not None
        assert tombed_b is not None
        # Read the raw artifacts, not ``_find_existing``: a mis-mapped entry
        # is re-resolved by scan and silently repaired on first lookup
        # (#1083), so a lookup-based assertion cannot tell "recorded
        # correctly" apart from "recorded wrong and healed on the way out".
        recorded = _raw_id_index(vault / "10-Liminal" / "Orphaned")
        assert recorded["frag-coll-a"] == tombed_a.name
        assert recorded["frag-coll-b"] == tombed_b.name
        assert recorded["frag-coll-a"] != recorded["frag-coll-b"]

        provenance = _provenance_paths(vault)
        assert provenance["frag-coll-a"] == str(tombed_a)
        assert provenance["frag-coll-b"] == str(tombed_b)

    def test_tomb_drops_the_origin_index_entry(self, tmp_path: Path) -> None:
        """Tombing removes the id from the origin directory's id index."""
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        writer.write_fragment(
            _colliding_fragment("frag-coll-a", SourcePlatform.JOURNAL),
            body=_MARKER_A,
        )
        origin_dir = vault / "01-Fragments" / "Journal"
        # Deliberately white-box. A tomb leaves the origin index pointing at a
        # filename it just unlinked; ``_find_existing_locked`` self-heals that
        # lazily on the next lookup, so an eager drop is indistinguishable from
        # the status quo through the public surface. Reading the index directly
        # is the only assertion that tells them apart, and the writer already
        # documents ``_find_existing`` as retained "for tests and tooling".
        assert "frag-coll-a" in writer._dir_indexes[origin_dir]

        writer.tomb_fragment("frag-coll-a")

        assert "frag-coll-a" not in writer._dir_indexes[origin_dir]

    def test_a_failed_destination_create_leaves_the_source_intact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tomb that cannot allocate a destination must not unlink the source.

        Pins the create-before-unlink ordering. ``_atomic_create`` gained two
        ways to raise that ``_atomic_write_text`` never had — ``RuntimeError``
        at counter-suffix exhaustion and ``OSError`` from a short write that
        ``create_exclusive`` cleans up. Unlink-first would turn either into a
        window where the fragment exists nowhere; create-first makes the worst
        case a recoverable duplicate.
        """
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        frag = _colliding_fragment("frag-coll-doomed", SourcePlatform.JOURNAL)
        live = writer.write_fragment(frag, body=_MARKER_A)
        before = live.read_bytes()

        def _exhausted(_target_dir: Path, _base_name: str, _content: str) -> Path:
            """Stand in for a destination allocator that cannot succeed."""
            msg = "Could not allocate a unique filename"
            raise RuntimeError(msg)

        monkeypatch.setattr(VaultWriter, "_atomic_create", staticmethod(_exhausted))

        with pytest.raises(RuntimeError):
            writer.tomb_fragment("frag-coll-doomed")

        # The source survives untouched: loud failure, no data loss.
        assert live.exists()
        assert live.read_bytes() == before
        assert _orphan_files(vault) == []

    def test_a_failed_restore_create_leaves_the_tombstone_intact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The restore direction keeps its source on a failed allocation too.

        Both directions share ``_relocate_fragment_locked``, so this is the
        symmetric half of the ordering guarantee asserted above — held
        explicitly rather than by inference from the tomb path.
        """
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        frag = _colliding_fragment("frag-coll-doomed", SourcePlatform.JOURNAL)
        writer.write_fragment(frag, body=_MARKER_A)
        tombed = writer.tomb_fragment("frag-coll-doomed")
        assert tombed is not None  # setup guard
        before = tombed.read_bytes()

        def _exhausted(_target_dir: Path, _base_name: str, _content: str) -> Path:
            """Stand in for a destination allocator that cannot succeed."""
            msg = "Could not allocate a unique filename"
            raise RuntimeError(msg)

        monkeypatch.setattr(VaultWriter, "_atomic_create", staticmethod(_exhausted))

        with pytest.raises(RuntimeError):
            writer.restore_fragment(frag)

        assert tombed.exists()
        assert tombed.read_bytes() == before
        assert _journal_files(vault) == []

    def test_round_trip_without_collision_keeps_the_original_filename(
        self, tmp_path: Path
    ) -> None:
        """A tomb+restore with no competitor preserves the existing filename.

        Expected to PASS at HEAD — this is a lock, not a RED. It pins the
        surviving half of the contract so a collision fix that recomputes the
        stem from the fragment (``_compute_base_name``) instead of preserving
        ``existing.name`` is caught: the title is drifted between the tomb and
        the restore, so a recomputed name would differ and silently rename the
        file out from under every ``[[wikilink]]`` pointing at it.
        """
        vault = _make_vault(tmp_path)
        writer = VaultWriter(vault_path=vault)
        frag = _colliding_fragment("frag-coll-solo", SourcePlatform.JOURNAL)
        original_name = writer.write_fragment(frag, body=_MARKER_A).name
        assert original_name == _COLLIDING_STEM

        tombed = writer.tomb_fragment("frag-coll-solo")
        assert tombed is not None
        assert tombed.name == original_name

        frag.title = "Notes Retitled Much Later"
        restored = writer.restore_fragment(frag)

        assert restored is not None
        assert restored.name == original_name
        assert restored.parent == vault / "01-Fragments" / "Journal"
        assert _MARKER_A in restored.read_text(encoding="utf-8")


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

    def test_reappear_with_unchanged_content_restores_without_rewrite(
        self, tmp_path: Path
    ) -> None:
        """A re-added unit with identical content is restored, not rewritten."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        entry = journal / "2026-06-26.md"
        body = "---\ndate: 2026-06-26\n---\nUnchanging entry.\n"
        entry.write_text(body, encoding="utf-8")
        _ingest(vault, journal)
        frag_id = frontmatter.load(str(_journal_files(vault)[0]))["id"]

        entry.unlink()
        _ingest(vault, journal)  # tomb
        assert len(_orphan_files(vault)) == 1

        # Re-add with IDENTICAL content -> restore-only branch (no rewrite).
        entry.write_text(body, encoding="utf-8")
        _, errors, _ = _ingest(vault, journal)
        assert errors == []
        live = _journal_files(vault)
        assert len(live) == 1
        post = frontmatter.load(str(live[0]))
        assert post["id"] == frag_id
        assert "Unchanging entry" in post.content
        assert _orphan_files(vault) == []

    def test_reappear_after_orphan_file_lost_recreates_with_preserved_id(
        self, tmp_path: Path
    ) -> None:
        """If the tombstone file vanished, a re-add recreates under the same id."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        entry = journal / "2026-06-26.md"
        entry.write_text("---\ndate: 2026-06-26\n---\nOriginal.\n", encoding="utf-8")
        _ingest(vault, journal)
        frag_id = frontmatter.load(str(_journal_files(vault)[0]))["id"]

        entry.unlink()
        _ingest(vault, journal)  # tomb
        # The tombstone file is lost out of band, but the ledger still says tombed.
        _orphan_files(vault)[0].unlink()

        entry.write_text("---\ndate: 2026-06-26\n---\nReborn.\n", encoding="utf-8")
        _, errors, _ = _ingest(vault, journal)
        assert errors == []
        live = _journal_files(vault)
        assert len(live) == 1
        post = frontmatter.load(str(live[0]))
        assert post["id"] == frag_id  # fresh write under the preserved id
        assert "Reborn" in post.content
        ledger = SourceLedger.load(vault, source="markdown")
        rec = ledger.get("personal/journal/2026-06-26.md")
        assert rec is not None
        assert rec.tombed is False  # ledger self-corrected

    def test_tomb_oserror_is_collected_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tomb that fails on I/O is collected as an error, not propagated."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        entry = journal / "2026-06-26.md"
        entry.write_text("---\ndate: 2026-06-26\n---\nDoomed.\n", encoding="utf-8")
        _ingest(vault, journal)

        def _boom(_self: VaultWriter, _fragment_id: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(VaultWriter, "tomb_fragment", _boom)
        entry.unlink()
        _, errors, _ = _ingest(vault, journal)  # must not raise

        assert any("failed to tomb" in e for e in errors)
        # Tomb failed -> the fragment is left live, ledger keeps it untombed.
        assert len(_journal_files(vault)) == 1
        ledger = SourceLedger.load(vault, source="markdown")
        rec = ledger.get("personal/journal/2026-06-26.md")
        assert rec is not None
        assert rec.tombed is False
