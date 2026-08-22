"""Ledger backing and tombing authority are independent (#1363, #1329).

RED-FIRST. #1363 widens ledger-backed identity from ``{markdown}`` to
``{markdown, generic, document}``. Ledger backing is what earns a fragment its
``source.origin_key``, and therefore its coverage by the RTBF purge sweep and
its idempotent re-ingest. Soft-deletion is a *different* authority: a
directory listing is only the authoritative set of live units for a source
whose whole corpus lives under that directory, which is true of the markdown
journal and of nothing else being widened here.

The two were already split by #1329 — :func:`tomb_missing_units` gates on the
ingestor's registry key, not on whether a ledger was resolved — so this file
is the *behavioural* proof of that split under the new, wider ledger set. Two
docstrings still assert the old coupling and are wrong (``run_ingest``'s
``ledger_source`` note and ``creek_mcp/tools/ingest.py``'s module docstring);
those are prose, and prose is not a gate. These assertions are.

Every test here is red at HEAD for the ledger half — ``ledger_for_source``
returns ``None`` for every source type but ``markdown``, so a generic or
document run writes no ledger file and stamps no ``origin_key``. The tombing
half is asserted in the same test on purpose: "does not tomb" proves nothing
about a source type that was never ledgered in the first place, so the
conjunction is the requirement.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import TOMBING_SOURCES, run_ingest

if TYPE_CHECKING:
    from pathlib import Path

# The source types #1363 newly ledgers. Registry keys are singular — the
# registry spells them ``document``/``generic``, not ``documents``.
_NEWLY_LEDGERED: tuple[tuple[str, str], ...] = (
    ("generic", ".log"),
    ("document", ".txt"),
)

_PINNED_MTIME = datetime(2024, 3, 15, 14, 30, tzinfo=UTC)


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


def _pin_mtime(path: Path) -> None:
    """Pin *path*'s mtime so the ingestor's derived timestamp is deterministic."""
    epoch = _PINNED_MTIME.timestamp()
    os.utime(path, (epoch, epoch))


def _live_fragments(vault: Path) -> list[Path]:
    """Return every live fragment file under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _orphans(vault: Path) -> list[Path]:
    """Return every soft-tombed fragment file under ``10-Liminal/Orphaned``."""
    return sorted((vault / "10-Liminal" / "Orphaned").rglob("*.md"))


def _raw_ledger_keys(vault: Path, source: str) -> set[str]:
    """Read ``source_key`` off the ledger's raw JSONL lines.

    Read raw rather than through :class:`~creek.ingest.ledger.SourceLedger`:
    a loader that skips or repairs a malformed line would report a healthier
    ledger than the one on disk, and the bytes are the claim being made.
    """
    path = vault / "00-Creek-Meta" / "State" / "ingest" / f"{source}.jsonl"
    if not path.exists():
        return set()
    return {
        str(json.loads(line)["source_key"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _origin_keys(vault: Path) -> set[str | None]:
    """Read ``source.origin_key`` off every live fragment's frontmatter."""
    keys: set[str | None] = set()
    for path in _live_fragments(vault):
        source = frontmatter.load(path).get("source")
        keys.add(source.get("origin_key") if isinstance(source, dict) else None)
    return keys


def test_the_widened_ledger_set_is_exactly_markdown_generic_and_document() -> None:
    """The ledger set is pinned, and it is not the tombing set.

    RED at HEAD: ``LEDGERED_SOURCES`` does not exist. Imported inside the test
    body so its absence fails this test alone rather than collecting the whole
    module as an error.

    ``image``, ``code``, ``spreadsheet``, ``presentation`` and the append-only
    export ingestors are deliberately excluded: each needs its own idempotency
    proof, and ``spreadsheet`` interacts with the #1305 sub-unit path.
    """
    from creek.ingest.pipeline import LEDGERED_SOURCE_TYPE, LEDGERED_SOURCES

    assert frozenset({"markdown", "generic", "document"}) == LEDGERED_SOURCES
    assert LEDGERED_SOURCE_TYPE == "markdown", (
        "LEDGERED_SOURCE_TYPE is the subject of unpinned_vault_warning and of "
        "the pin_ids migration; both are markdown-specific and neither may be "
        "widened by this change."
    )
    assert {key for key, _ in _NEWLY_LEDGERED} == LEDGERED_SOURCES - {
        LEDGERED_SOURCE_TYPE
    }, "the parametrised cases below no longer cover every newly ledgered type."


def test_tombing_authority_is_still_exactly_markdown() -> None:
    """Widening the ledger must not widen who may soft-delete.

    Green at HEAD by construction, and it stays green: this is the migration
    guard for the tombing half. ``tests/test_ingest_orphan_tomb.py`` pins the
    same set and is left untouched.
    """
    assert frozenset({"markdown"}) == TOMBING_SOURCES


@pytest.mark.parametrize(("source_type", "suffix"), _NEWLY_LEDGERED)
def test_a_newly_ledgered_directory_pass_records_units_but_tombs_none(
    tmp_path: Path, source_type: str, suffix: str
) -> None:
    """A ledgered non-markdown source keeps its records and deletes nothing.

    Two ingests: the first ledgers two units; the second runs over the same
    directory with one of them deleted. Being ledgered, the pass now *could*
    compute a gone set — and must refuse to, because a document directory is
    not the authoritative set of live document fragments (an operator moves a
    PDF out of a staging folder without meaning to orphan its fragment).

    RED at HEAD on the ledger half: ``ledger_for_source`` returns ``None`` for
    both source types, so nothing is recorded and no ``origin_key`` is
    stamped. The tombing assertions are the half that must survive the fix.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    keep = src / f"keep{suffix}"
    gone = src / f"gone{suffix}"
    for path in (keep, gone):
        path.write_text(f"Body of {path.name}.\n", encoding="utf-8")
        _pin_mtime(path)

    first = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY[source_type],
        source_type=source_type,
        input_path=src,
        vault_path=vault,
    )
    assert first.errors == []
    assert first.created == 2
    assert len(_raw_ledger_keys(vault, source_type)) == 2, (
        f"a {source_type} ingest wrote no ledger records, so its fragments "
        "carry no source.origin_key and the RTBF purge sweep cannot see them."
    )
    assert None not in _origin_keys(vault)

    gone.unlink()
    second = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY[source_type],
        source_type=source_type,
        input_path=src,
        vault_path=vault,
    )

    assert second.errors == []
    assert second.tombed == 0, (
        f"a ledgered {source_type} directory pass soft-tombed {second.tombed} "
        "fragment(s). Holding a ledger is identity, not deletion authority."
    )
    assert _orphans(vault) == []
    assert len(_live_fragments(vault)) == 2
    assert len(_raw_ledger_keys(vault, source_type)) == 2


def test_a_borrowed_ledger_is_never_swept_by_the_borrowing_pass(
    tmp_path: Path,
) -> None:
    """A pass that borrows another source's ledger may not tomb from it.

    ``run_ingest(source_type="markdown", ledger_source="upload")`` passes the
    ``TOMBING_SOURCES`` gate on its *own* registry key while the ledger being
    swept belongs to ``upload``, whose units this pass never enumerates. The
    gate has to be a conjunction: only tomb when the ledger is the one this
    source type owns.

    RED at HEAD: the borrowed ledger's record is swept and its fragment is
    relocated into ``10-Liminal/Orphaned/``. No caller does this today, which
    is why it is closed by construction rather than left to a comment.
    """
    from creek.ingest.ledger import SourceLedger

    vault = _make_vault(tmp_path)
    journal = tmp_path / "journal"
    journal.mkdir()
    entry = journal / "2024-01-05.md"
    entry.write_text("---\ndate: 2024-01-05\n---\n\n# Day\n\nBody.\n", encoding="utf-8")
    _pin_mtime(entry)

    seeded = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=entry,
        vault_path=vault,
        ledger_source="upload",
    )
    assert seeded.errors == []
    assert len(SourceLedger.load(vault, source="upload")) == 1

    other = tmp_path / "other"
    other.mkdir()
    unrelated = other / "2024-02-01.md"
    unrelated.write_text(
        "---\ndate: 2024-02-01\n---\n\n# Other\n\nBody.\n", encoding="utf-8"
    )
    _pin_mtime(unrelated)

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=other,
        vault_path=vault,
        ledger_source="upload",
    )

    assert result.errors == []
    assert result.tombed == 0, (
        f"a borrowed ledger was swept by a pass that never enumerated its "
        f"units: {result.tombed} fragment(s) tombed."
    )
    assert _orphans(vault) == []
