"""Out-of-vault source identity: two files, two fragments (#953, #1363).

RED-FIRST. ``derive_source_key`` falls back to ``candidate.name`` — a bare
filename — for any source that is not relative to the vault root
(``creek/ingest/pipeline.py``, the ``except ValueError`` arm). Two same-named
files in different out-of-vault directories therefore collide on one
``source_key``, and the collision is not a duplicate: it is **content
destruction**. ``write_fragment_idempotent`` hoists
``fragment.id = record.fragment_id`` above every branch, so the second file
adopts the first's id and rewrites its body.

Measured at HEAD before these assertions were written, so that none of them
can be phrased in a way that is red for an unrelated reason:

    <tmp>/journal/a/notes.md  (body MARKER-ALPHA)
    <tmp>/journal/b/notes.md  (body MARKER-BRAVO)
    run_ingest(source_type="markdown", input_path=<tmp>/journal)
      -> written 2, created 1, updated 1, errors []
      -> fragment_ids ['frag-b73322f3e3ab', 'frag-b73322f3e3ab']   # ONE id
      -> 1 fragment on disk, holding MARKER-BRAVO.  ALPHA is gone.
    ledger markdown.jsonl: two lines, both source_key "notes.md"

This is live today for every synced vault: ``creek sync`` ingests the
``journal`` source as source_type ``markdown`` from
``config.source_drive / "personal/journal/"`` — an out-of-vault directory,
walked recursively, enabled by default.

The ``document`` half (#1363) is the same defect one step earlier. Documents
are not ledgered at all today, so they never receive ``source.origin_key``,
are invisible to the RTBF purge sweep (which keys on exactly that field), and
re-ingest non-idempotently. Measured at HEAD:

    <tmp>/src/a/report.txt, <tmp>/src/b/report.txt
    run_ingest(source_type="document", ...) -> 2 fragments, origin_key None
    the same run with ledger_source="document" -> 1 fragment, ALPHA destroyed

so widening the ledger to documents *without* fixing the key would import the
data-loss defect wholesale. Both halves have to land together, which is why
they are asserted together here.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import derive_source_key, run_ingest

if TYPE_CHECKING:
    from pathlib import Path

_ALPHA = "MARKER-ALPHA-7f21"
_BRAVO = "MARKER-BRAVO-3c94"

_PINNED_MTIME = datetime(2024, 3, 15, 14, 30, tzinfo=UTC)
_LATER_MTIME = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)


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


def _origin_keys(vault: Path) -> list[str | None]:
    """Read ``source.origin_key`` off every live fragment's frontmatter."""
    keys: list[str | None] = []
    for path in _live_fragments(vault):
        source = frontmatter.load(path).get("source")
        keys.append(source.get("origin_key") if isinstance(source, dict) else None)
    return keys


def _raw_ledger_keys(vault: Path, source: str) -> list[str]:
    """Read ``source_key`` off the ledger's raw JSONL lines, in file order."""
    path = vault / "00-Creek-Meta" / "State" / "ingest" / f"{source}.jsonl"
    if not path.exists():
        return []
    return [
        str(json.loads(line)["source_key"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _two_sources(tmp_path: Path, name: str, *, body_prefix: str) -> Path:
    """Write ``<tmp>/src/a/<name>`` and ``<tmp>/src/b/<name>`` with distinct bodies."""
    src = tmp_path / "src"
    for folder, marker in (("a", _ALPHA), ("b", _BRAVO)):
        target = src / folder
        target.mkdir(parents=True, exist_ok=True)
        file = target / name
        file.write_text(f"{body_prefix}\n\n{marker} content body.\n", encoding="utf-8")
        _pin_mtime(file)
    return src


# ---- The key itself --------------------------------------------------------


def test_two_out_of_vault_sources_with_one_basename_get_two_keys(
    tmp_path: Path,
) -> None:
    """A bare filename is not an identity; the parent directory must count.

    RED at HEAD: both sides return ``"report.pdf"``.
    """
    vault = _make_vault(tmp_path)
    first = tmp_path / "a" / "report.pdf"
    second = tmp_path / "b" / "report.pdf"
    for path in (first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")

    key_a = derive_source_key(str(first), vault)
    key_b = derive_source_key(str(second), vault)

    assert key_a != key_b, (
        f"two different files share one source_key {key_a!r}. The ledger maps "
        "a key to one fragment id, so the second ingest overwrites the first."
    )
    assert key_a.endswith("/report.pdf"), (
        f"the key {key_a!r} no longer names the file. An operator reading the "
        "ledger or the frontmatter has to be able to recognise the source."
    )
    assert key_b.endswith("/report.pdf")


def test_an_out_of_vault_key_is_deterministic_and_resolves_symlinks(
    tmp_path: Path,
) -> None:
    """One real file yields one key, however it is spelled.

    RED at HEAD for the symlink half: the fallback returns the *link's* own
    basename, so a link and its target key differently and one real file
    becomes two fragments.
    """
    vault = _make_vault(tmp_path)
    real = tmp_path / "real" / "notes.md"
    real.parent.mkdir(parents=True)
    real.write_text("body\n", encoding="utf-8")
    link = tmp_path / "real" / "alias.md"
    link.symlink_to(real)

    assert derive_source_key(str(real), vault) == derive_source_key(str(real), vault)
    assert derive_source_key(str(link), vault) == derive_source_key(str(real), vault), (
        "a symlink and its target keyed differently, so one file would be "
        "ingested as two fragments."
    )


def test_legacy_source_key_names_the_pre_fix_bare_filename(tmp_path: Path) -> None:
    """The migration needs a single source of truth for the old spelling.

    RED at HEAD: :func:`creek.ingest.pipeline.legacy_source_key` does not
    exist. It must answer ``None`` for anything in-vault — an in-vault key
    never had a legacy form, and that is what makes the adoption machinery
    structurally unable to touch the in-vault corpus even by accident.
    """
    from creek.ingest.pipeline import legacy_source_key

    vault = _make_vault(tmp_path)
    outside = tmp_path / "drive" / "journal" / "2024-01-05.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("body\n", encoding="utf-8")
    inside = vault / "01-Fragments" / "Notes" / "kept.md"
    inside.write_text("body\n", encoding="utf-8")

    assert legacy_source_key(str(outside), vault) == "2024-01-05.md"
    assert legacy_source_key(str(inside), vault) is None, (
        "an in-vault source was reported as having a legacy key. Nothing in "
        "the migration may reach the in-vault corpus."
    )


# ---- Markdown: the live data-loss path -------------------------------------


def test_two_same_named_markdown_sources_do_not_destroy_each_other(
    tmp_path: Path,
) -> None:
    """Two journal entries with one filename keep both bodies.

    RED at HEAD, measured: one fragment survives, holding MARKER-BRAVO;
    MARKER-ALPHA's content is gone from the vault entirely, and both ledger
    lines are keyed ``"notes.md"``. This is ``creek sync``'s journal pass.
    """
    vault = _make_vault(tmp_path)
    src = _two_sources(tmp_path, "notes.md", body_prefix="# Notes")

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )

    assert result.errors == []
    assert result.discovered == 2
    assert (result.created, result.updated) == (2, 0), (
        f"created={result.created} updated={result.updated}: the second file "
        "was treated as an edit of the first."
    )
    assert len(set(result.fragment_ids)) == 2, (
        f"both files were written under one id {result.fragment_ids}."
    )
    live = _live_fragments(vault)
    assert len(live) == 2, f"one document was overwritten by the other: {live}"
    text = _bodies(vault)
    assert _ALPHA in text, "the first document's body was destroyed."
    assert _BRAVO in text
    assert len(set(_raw_ledger_keys(vault, "markdown"))) == 2


# ---- Documents: ledger-backed identity (#1363) -----------------------------


def test_two_same_named_documents_ingest_as_two_ledgered_fragments(
    tmp_path: Path,
) -> None:
    """Documents get ledger-backed identity, and it distinguishes them.

    RED at HEAD on ``origin_key``: ``ledger_for_source`` returns ``None`` for
    every source type but ``markdown``, so a document fragment carries no
    ``source.origin_key`` and is invisible to the RTBF purge sweep, which
    keys on that field. Turning the ledger on without the key fix would
    instead collapse these two into one — measured — so both are asserted
    in one test.
    """
    vault = _make_vault(tmp_path)
    src = _two_sources(tmp_path, "report.txt", body_prefix="Quarterly notes.")

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["document"],
        source_type="document",
        input_path=src,
        vault_path=vault,
    )

    assert result.errors == []
    assert (result.created, result.updated) == (2, 0)
    assert len(_live_fragments(vault)) == 2
    text = _bodies(vault)
    assert _ALPHA in text
    assert _BRAVO in text
    keys = _origin_keys(vault)
    assert None not in keys, (
        f"a document fragment carries no source.origin_key: {keys}. The RTBF "
        "purge sweep resolves its targets from that field and skips fragments "
        "without it."
    )
    assert len(set(keys)) == 2, f"both documents share one origin_key: {keys}"
    assert len(set(_raw_ledger_keys(vault, "document"))) == 2


def test_an_edited_document_updates_in_place_under_its_preserved_id(
    tmp_path: Path,
) -> None:
    """Re-ingesting an edited document updates it; it does not duplicate it.

    RED at HEAD: with no ledger the edited file re-derives a fresh id from its
    new mtime and content, so the vault ends with two fragments and the
    predecessor is orphaned.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    doc = src / "report.txt"
    doc.write_text(f"Quarterly notes.\n\n{_ALPHA} first draft.\n", encoding="utf-8")
    _pin_mtime(doc)

    first = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["document"],
        source_type="document",
        input_path=src,
        vault_path=vault,
    )
    assert first.errors == []
    assert first.created == 1

    doc.write_text(f"Quarterly notes.\n\n{_BRAVO} revised draft.\n", encoding="utf-8")
    _pin_mtime(doc, _LATER_MTIME)
    second = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["document"],
        source_type="document",
        input_path=src,
        vault_path=vault,
    )

    assert second.errors == []
    assert (second.created, second.updated) == (0, 1), (
        f"created={second.created} updated={second.updated}: the edit minted a "
        "new fragment instead of rewriting the existing one."
    )
    assert second.fragment_ids == first.fragment_ids
    live = _live_fragments(vault)
    assert len(live) == 1, f"the edit duplicated the document: {live}"
    assert _BRAVO in _bodies(vault)


# ---- Generic: the same idempotency, for the fallback ingestor (#953) -------


def test_a_touched_generic_file_is_unchanged_under_its_preserved_id(
    tmp_path: Path,
) -> None:
    """An mtime bump with identical bytes is not a new fragment.

    RED at HEAD: ``generic`` is unledgered, so its id derives from the mtime
    (#911) and a ``touch`` alone mints a second fragment.

    ``tests/test_ingest_generic_idempotent.py::test_changed_mtime_yields_new_id``
    stays untouched and green — it asserts at the *derivation* level
    (``ingestor.parse`` + ``_assemble_fragment_id``), which ledger-backed
    identity at the pipeline level does not change.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    note = src / "note.log"
    note.write_text(f"{_ALPHA} log line.\n", encoding="utf-8")
    _pin_mtime(note)

    first = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["generic"],
        source_type="generic",
        input_path=src,
        vault_path=vault,
    )
    assert first.errors == []
    assert first.created == 1

    _pin_mtime(note, _LATER_MTIME)
    second = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["generic"],
        source_type="generic",
        input_path=src,
        vault_path=vault,
    )

    assert second.errors == []
    assert (second.created, second.unchanged) == (0, 1), (
        f"created={second.created} unchanged={second.unchanged}: a touched "
        "file with identical bytes was ingested as a new fragment."
    )
    assert second.fragment_ids == first.fragment_ids
    assert len(_live_fragments(vault)) == 1
