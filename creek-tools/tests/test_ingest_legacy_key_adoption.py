"""Migrating out-of-vault markdown to the new key without moving an id (#953).

RED-FIRST, and the population the brief did not know about. ``creek sync``
ingests the ``journal`` source as source_type ``markdown`` (``creek/pipeline.py``
maps ``"journal": "markdown"``), it is enabled by default, and its input is
``config.source_drive / "personal/journal/"`` — an **external drive path**,
walked recursively. Every synced vault therefore already holds markdown ledger
records keyed by *bare filename*.

So the change's key fix does not only affect a hypothetical future ingest: it
re-keys live records. Left alone, the next ``creek sync`` would miss on the new
key, take the ``record is None`` branch, mint a fresh id and orphan every
journal fragment in the vault — the #1304/#1329/#1384 scar again, at corpus
scale.

The mitigation is **adoption**: on a miss under the new key, look up the legacy
key; if it maps to a fragment whose own ``source.original_file`` resolves to
*this exact source path*, re-record it under the new key with the same id. The
proof is exact rather than heuristic — the fragment on disk names its own
source verbatim — which is what stops adoption from blessing the wrong file
when two sources used to share one key.

The companion guards live in ``tests/test_ingest_source_key_identity.py``: a
present source must not be tombed via its stale legacy key, and a genuinely
deleted one must still tomb exactly once.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.ingest import INGESTOR_REGISTRY, assemble_ingested_fragment
from creek.ingest.ledger import SourceLedger
from creek.ingest.pin_ids import pin_source_ids
from creek.ingest.pipeline import derive_source_key, run_ingest
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

_LEGACY_ID = "frag-legacy00001"
_OTHER_ID = "frag-legacy00002"
_PINNED_MTIME = datetime(2024, 1, 5, 8, 30, tzinfo=UTC)
_LATER_MTIME = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)

_ORIGINAL = "---\ndate: 2024-01-05\n---\n\n# Morning\n\nOriginal body.\n"
_EDITED = "---\ndate: 2024-01-05\n---\n\n# Morning\n\nEdited, MARKER-EDIT.\n"
_RIVAL = "---\ndate: 2024-01-05\n---\n\n# Morning\n\nRival, MARKER-RIVAL.\n"


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree ``VaultWriter`` and the ledger need."""
    vault = tmp_path / "vault"
    for relative in (
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/State/ingest",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "01-Fragments/Unsorted",
        "10-Liminal/Orphaned",
    ):
        (vault / relative).mkdir(parents=True, exist_ok=True)
    return vault


def _pin_mtime(path: Path, moment: datetime = _PINNED_MTIME) -> None:
    """Pin *path*'s mtime so the ingestor's derived timestamp is deterministic."""
    epoch = moment.timestamp()
    os.utime(path, (epoch, epoch))


def _live_fragments(vault: Path) -> list[Path]:
    """Return every live fragment file under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _bodies(vault: Path) -> str:
    """Concatenate every live fragment's text, for marker containment checks."""
    return "\n".join(p.read_text(encoding="utf-8") for p in _live_fragments(vault))


def _fragment_ids_on_disk(vault: Path) -> set[str]:
    """Read the ``id`` off every live fragment's frontmatter."""
    return {str(frontmatter.load(p)["id"]) for p in _live_fragments(vault)}


def _raw_ledger_keys(vault: Path) -> set[str]:
    """Read ``source_key`` off the markdown ledger's raw JSONL lines."""
    path = vault / "00-Creek-Meta" / "State" / "ingest" / "markdown.jsonl"
    if not path.exists():
        return set()
    return {
        str(json.loads(line)["source_key"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _seed_legacy_state(
    vault: Path, source: Path, *, fragment_id: str, legacy_key: str
) -> None:
    """Seed the vault state a pre-#953 ``creek sync`` journal pass left behind.

    The fragment is assembled by the real ingestor and only then re-stamped,
    so it lands in the directory a re-ingest will look in and records the same
    ``source.original_file`` a real run would — which is exactly the field
    adoption demands as proof.
    """
    ingested = INGESTOR_REGISTRY["markdown"]().ingest(source)
    assembled = assemble_ingested_fragment(ingested.fragments[0])
    assembled.fragment.id = fragment_id
    assembled.fragment.source.origin_key = legacy_key
    VaultWriter(vault_path=vault).write_fragment(
        assembled.fragment, body=assembled.body
    )
    ledger = SourceLedger.load(vault, source="markdown")
    ledger.record(legacy_key, fragment_id, "0" * 64)


def test_a_legacy_bare_filename_record_is_adopted_and_the_id_never_moves(
    tmp_path: Path,
) -> None:
    """A re-keyed journal entry keeps the id its vault already holds.

    RED at HEAD at the first assertion: the key has not moved yet, so there is
    nothing to adopt. Once it moves, the rest of this test is the whole
    migration contract — one fragment, the same id, the new key recorded.
    """
    vault = _make_vault(tmp_path)
    journal = tmp_path / "drive" / "personal" / "journal"
    journal.mkdir(parents=True)
    source = journal / "2024-01-05.md"
    source.write_text(_ORIGINAL, encoding="utf-8")
    _pin_mtime(source)
    _seed_legacy_state(
        vault, source, fragment_id=_LEGACY_ID, legacy_key="2024-01-05.md"
    )

    new_key = derive_source_key(str(source), vault)
    assert new_key != "2024-01-05.md", (
        "the out-of-vault key is still a bare filename, so two journal files "
        "with one name still destroy each other (#953)."
    )

    source.write_text(_EDITED, encoding="utf-8")
    _pin_mtime(source, _LATER_MTIME)
    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=journal,
        vault_path=vault,
    )

    assert result.errors == []
    assert result.fragment_ids == [_LEGACY_ID], (
        f"the re-keyed source minted a new id {result.fragment_ids}, orphaning "
        f"{_LEGACY_ID}. Every synced vault's journal corpus is this case."
    )
    assert (result.created, result.updated) == (0, 1)
    assert result.tombed == 0
    live = _live_fragments(vault)
    assert len(live) == 1, f"the migration duplicated the corpus: {live}"
    assert "MARKER-EDIT" in _bodies(vault)
    assert new_key in _raw_ledger_keys(vault), (
        "the new key was never recorded, so the next run re-adopts forever "
        "instead of converging."
    )


def test_adoption_never_steals_another_files_record(tmp_path: Path) -> None:
    """Only the file the legacy record actually names may inherit its id.

    Two out-of-vault sources share the basename ``notes.md``; the legacy record
    was written for the one under ``x/``. The one under ``y/`` has no
    predecessor and must be created fresh — adopting there would silently
    graft one document's history onto another's content.

    RED at HEAD, measured: both files key ``"notes.md"``, so ``y`` adopts
    ``x``'s id, rewrites ``x``'s body, and the vault ends with one fragment.
    """
    vault = _make_vault(tmp_path)
    root = tmp_path / "drive" / "personal" / "journal"
    (root / "x").mkdir(parents=True)
    (root / "y").mkdir(parents=True)
    kept = root / "x" / "notes.md"
    rival = root / "y" / "notes.md"
    kept.write_text(_ORIGINAL, encoding="utf-8")
    _pin_mtime(kept)
    _seed_legacy_state(vault, kept, fragment_id=_LEGACY_ID, legacy_key="notes.md")
    rival.write_text(_RIVAL, encoding="utf-8")
    _pin_mtime(rival)

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=root,
        vault_path=vault,
    )

    assert result.errors == []
    live = _live_fragments(vault)
    assert len(live) == 2, (
        f"one of the two documents was destroyed: {live}. The rival adopted "
        "the record that names a different file."
    )
    ids = _fragment_ids_on_disk(vault)
    assert _LEGACY_ID in ids, f"the pre-existing id was re-minted: {ids}"
    assert len(ids) == 2, f"both files were written under one id: {ids}"
    text = _bodies(vault)
    assert "Original body." in text
    assert "MARKER-RIVAL" in text
    assert result.created == 1
    assert result.tombed == 0


def test_pin_ids_and_ingest_converge_whichever_runs_first(tmp_path: Path) -> None:
    """The migration is order-independent, and it converges.

    An operator may run ``creek ingest --pin-source-ids`` before their next
    sync or after it. Either order must end with the same key holding the same
    id, and the second tool must report nothing left to do rather than pinning
    a second record for the same fragment.

    Green at HEAD only because the key has not moved yet; it is the ordering
    guard for the fix. Its non-vacuousness is proven by mutation once adoption
    exists (make adoption record a *derived* id instead of the ledger's and
    the ``already_pinned`` arm goes red).
    """
    vault = _make_vault(tmp_path)
    journal = tmp_path / "drive" / "personal" / "journal"
    journal.mkdir(parents=True)
    source = journal / "2024-01-05.md"
    source.write_text(_ORIGINAL, encoding="utf-8")
    _pin_mtime(source)
    _seed_legacy_state(vault, source, fragment_id=_OTHER_ID, legacy_key="2024-01-05.md")

    ingested = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=journal,
        vault_path=vault,
    )
    assert ingested.errors == []
    assert ingested.fragment_ids == [_OTHER_ID]

    pinned = pin_source_ids(vault)

    assert pinned.conflicts == [], f"adoption left a contested key: {pinned.conflicts}"
    assert pinned.pinned == 0, (
        f"pin_source_ids wrote {pinned.pinned} record(s) for a fragment the "
        "ingest had already adopted; the two disagree about the key."
    )
    assert pinned.already_pinned == 1
    assert derive_source_key(str(source), vault) in _raw_ledger_keys(vault)
    assert len(_live_fragments(vault)) == 1


@pytest.mark.parametrize(
    ("rival_basename", "expected_tombed", "expected_live"),
    [("notes.md", 0, 2), ("other.md", 1, 1)],
)
def test_a_shared_legacy_basename_shields_a_deleted_source_for_one_run(
    tmp_path: Path,
    rival_basename: str,
    expected_tombed: int,
    expected_live: int,
) -> None:
    """A live neighbour sharing a deleted file's basename defers its tombing.

    PINNED AS INTENTIONAL, not filed as a defect (#1577).
    :func:`creek.ingest.pipeline.establish_source_identity` reports a unit's
    *legacy* key as seen unconditionally, and
    :func:`creek.ingest.pipeline.legacy_source_key` derives that key from
    ``candidate.name`` — the bare basename — for every out-of-vault source.
    Two out-of-vault files therefore share one legacy key, so the survivor's
    seen-report covers the deleted file's record and the tomb sweep passes
    over it.

    Making this exact would cost the vault-wide scan #1367 declined: proving
    which file a bare-basename record belongs to means reading every
    fragment's ``source.original_file`` on every run. The approximation is
    correctly *scoped* rather than merely cheap — the second arm shows a
    distinct basename still tombs on the very same run — and its cost is
    bounded to a missed soft-delete that the next run without the neighbour
    performs. Nothing is destroyed and nothing is mis-attributed.

    The two arms are one test on purpose. An assertion on the shared arm
    alone would still pass if tombing were disabled outright; the pair
    cannot.
    """
    vault = _make_vault(tmp_path)
    root = tmp_path / "drive" / "personal" / "journal"
    (root / "x").mkdir(parents=True)
    (root / "y").mkdir(parents=True)
    deleted = root / "x" / "notes.md"
    rival = root / "y" / rival_basename
    deleted.write_text(_ORIGINAL, encoding="utf-8")
    _pin_mtime(deleted)
    _seed_legacy_state(vault, deleted, fragment_id=_LEGACY_ID, legacy_key="notes.md")
    deleted.unlink()
    rival.write_text(_RIVAL, encoding="utf-8")
    _pin_mtime(rival)

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=root,
        vault_path=vault,
    )

    # The full tuple, not just ``tombed``: a change in the *shape* of the
    # seen-report — one that stopped creating the rival, or started erroring
    # — would otherwise slip past an assertion on the sweep alone.
    assert (result.tombed, result.created, result.errors) == (
        expected_tombed,
        1,
        [],
    )
    live = _live_fragments(vault)
    assert len(live) == expected_live, live
    assert "MARKER-RIVAL" in _bodies(vault)
