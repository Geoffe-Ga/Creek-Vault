"""The #1329 pin migration: existing ids must survive the derivation change.

``creek/ingest/pin_ids.py`` back-fills the markdown ingest ledger with each
existing fragment's **existing** id, so the corrected (host-independent)
derivation never moves an id that is already on disk.

The load-bearing test in this module is
:func:`test_a_bare_reingest_of_an_unpinned_vault_duplicates` — it demonstrates
the failure the migration prevents. Without it, every other assertion here
could hold while the migration was doing nothing useful, and a reader would
have no way to tell.

Vaults are seeded the way a real pre-fix vault looks: fragments written through
the real :class:`~creek.vault.writer.VaultWriter` under ids minted by the
**old naive derivation**, ``source.original_file`` populated,
``source.origin_key`` absent, and an empty ledger.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.ingest import pin_ids
from creek.ingest.base import generate_fragment_id
from creek.ingest.journal_staging import UPLOAD_LEDGER_SOURCE
from creek.ingest.ledger import SourceLedger
from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.pin_ids import pin_source_ids
from creek.ingest.pipeline import (
    IngestRunResult,
    run_ingest,
    unpinned_vault_warning,
)
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

PINNED_MTIME = 1710493200.0
"""2024-03-15T09:00:00+00:00."""

_BODY = "An untouched note.\n"

_PRE_FIX_ID = "frag-0ldc0ffee123"
"""A stand-in for an id minted by the pre-fix derivation.

Its exact value does not matter and it deliberately is **not** reproducible
from the fragment's contents: the whole point of pinning is that the migration
records the id it *finds*, never one it recomputes. A literal that happened to
reproduce would let a broken implementation pass.
"""


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree the writer needs.

    Args:
        tmp_path: Pytest temp directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for directory in ("00-Creek-Meta/Processing-Log", "01-Fragments/Unsorted"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    return vault


def _make_source(tmp_path: Path, name: str = "note.md", body: str = _BODY) -> Path:
    """Write a pinned-mtime markdown source file outside the vault.

    Args:
        tmp_path: Pytest temp directory.
        name: Filename to create.
        body: File contents.

    Returns:
        The source file path.
    """
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    note = src / name
    note.write_text(body, encoding="utf-8")
    os.utime(note, (PINNED_MTIME, PINNED_MTIME))
    return note


def _seed_pre_fix_fragment(
    vault: Path,
    source: Path,
    *,
    fragment_id: str = _PRE_FIX_ID,
    body: str = _BODY,
) -> Path:
    """Write one fragment as a pre-fix vault would hold it.

    ``source.origin_key`` is deliberately left unset — that is the state the
    migration has to repair, and the state
    :meth:`~creek.vault.writer.VaultWriter.update_fragment` can never repair
    on its own because it preserves on-disk frontmatter rather than merging
    fresh ``source`` fields into it.

    Args:
        vault: Vault root.
        source: The markdown source this fragment came from.
        fragment_id: The id to write, standing in for a pre-fix derivation.
        body: The stored fragment body.

    Returns:
        The written fragment's path.
    """
    fragment = Fragment(
        id=fragment_id,
        title=source.stem,
        created=datetime(2024, 3, 14, 19, 0, tzinfo=UTC),
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            original_file=str(source),
        ),
    )
    return VaultWriter(vault_path=vault).write_fragment(fragment, body=body)


def _fragment_files(vault: Path) -> list[Path]:
    """Return every fragment markdown file under ``01-Fragments``.

    Args:
        vault: Vault root.

    Returns:
        Sorted fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _origin_key(md_file: Path) -> str | None:
    """Read ``source.origin_key`` straight off disk, as the purge sweep does.

    Args:
        md_file: The fragment file.

    Returns:
        The recorded origin key, or ``None`` when absent.
    """
    source = frontmatter.load(str(md_file)).metadata.get("source")
    if not isinstance(source, dict):
        return None
    value = source.get("origin_key")
    return str(value) if value is not None else None


def _reingest(vault: Path, src_dir: Path) -> IngestRunResult:
    """Run a bare, non-incremental markdown ingest over *src_dir*.

    Args:
        vault: Vault root.
        src_dir: Directory holding the source files.

    Returns:
        The pipeline's structured result.
    """
    return run_ingest(
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        input_path=src_dir,
        vault_path=vault,
    )


# ---- (a) the failure being prevented ----


def test_a_bare_reingest_of_an_unpinned_vault_duplicates(tmp_path: Path) -> None:
    """Without the migration, re-ingesting a pre-fix vault orphans its fragment.

    This is what makes the migration load-bearing rather than ceremonial: the
    corrected derivation mints an id the vault has never seen, the empty
    ledger offers nothing to match on, and the pre-fix fragment is left behind.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)
    assert len(_fragment_files(vault)) == 1

    result = _reingest(vault, source.parent)

    assert result.created == 1
    assert len(_fragment_files(vault)) == 2


def test_an_unpinned_vault_advisory_is_recorded_in_the_run_result(
    tmp_path: Path,
) -> None:
    """The advisory reaches the structured result on the run that would hurt.

    Derived from the ledger's own emptiness rather than a separate version
    marker, so there is only one piece of state that can be wrong. This asserts
    the *result object* only; whether a human ever sees it is a separate
    property, covered by
    :func:`test_cli_ingest_prints_the_unpinned_vault_advisory_before_it_writes`.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    result = _reingest(vault, source.parent)

    assert any("--pin-source-ids" in warning for warning in result.warnings)


def test_a_fresh_vault_is_not_warned_about(tmp_path: Path) -> None:
    """An empty vault has nothing to strand, so the advisory stays quiet."""
    vault = _make_vault(tmp_path)

    assert unpinned_vault_warning("markdown", vault) is None


def test_a_pinned_vault_is_not_warned_about(tmp_path: Path) -> None:
    """Once the ledger has records the advisory stops firing."""
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)
    pin_source_ids(vault)

    assert unpinned_vault_warning("markdown", vault) is None


# ---- (a2) the advisory's subject is the markdown ledger, always ----


def _upload_style_ingest(vault: Path, staged: Path) -> list[str]:
    """Ingest one markdown file the way ``creek.upload`` does, and collect its
    advisories from the emission channel operators actually see.

    ``creek_mcp.tools.upload`` always passes
    ``ledger_source=UPLOAD_LEDGER_SOURCE``, including for an uploaded ``.md``
    (``route_to_ingestor`` maps ``.md`` to the *markdown* ingestor), so this is
    the exact shape of a real upload: markdown identity written under a
    borrowed ledger. A single file is passed, not a directory, because that is
    what the upload tool passes — and because a directory pass under a borrowed
    ledger is the separate tombing hazard :data:`TOMBING_SOURCES` guards.

    Advisories are read off the ``on_warning`` callback rather than
    :attr:`IngestRunResult.warnings`: the callback is the channel a surface
    prints from, so asserting on it tests the advisory as *emitted* rather than
    as an internal tally.

    Args:
        vault: Vault root.
        staged: The single markdown file standing in for a staged upload.

    Returns:
        Every advisory emitted during the run, in emission order.
    """
    emitted: list[str] = []
    run_ingest(
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        input_path=staged,
        vault_path=vault,
        ledger_source=UPLOAD_LEDGER_SOURCE,
        on_warning=emitted.append,
    )
    return emitted


def test_a_pinned_vault_is_not_warned_about_through_a_borrowed_ledger(
    tmp_path: Path,
) -> None:
    """An already-pinned vault stays quiet even when the run borrows a ledger.

    The false-positive half of the ``ledger_source`` defect. This vault *is*
    migrated — its markdown ledger holds the pinned record — but the first
    upload finds the ``upload`` ledger empty, and an advisory that reads
    whichever ledger the run happened to resolve would tell the operator to
    re-run a migration they have already run. An advisory that fires on a vault
    in good order is how operators learn to ignore advisories.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)
    pin_source_ids(vault)

    emitted = _upload_style_ingest(vault, source)

    assert [msg for msg in emitted if "--pin-source-ids" in msg] == []


def test_an_unpinned_vault_is_warned_about_through_a_borrowed_ledger(
    tmp_path: Path,
) -> None:
    """An un-pinned vault is still warned about once an unrelated ledger fills.

    The false-negative half, and the serious one: the markdown ledger is empty
    — the vault genuinely will duplicate on its next markdown re-ingest — but a
    previous upload has put a record in the ``upload`` ledger. Reading the
    resolved ledger's emptiness silences the advisory through this path
    permanently, on exactly the vault that needs it. The advisory's subject is
    the markdown ledger; nothing a run overrides may substitute another.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)
    SourceLedger.load(vault, source=UPLOAD_LEDGER_SOURCE).record(
        "00-Creek-Meta/Staging/uploads/earlier.pdf",
        "frag-unrelated-upload",
        SourceLedger.content_hash("earlier"),
    )

    emitted = _upload_style_ingest(vault, source)

    assert [msg for msg in emitted if "--pin-source-ids" in msg] != []


def test_a_non_markdown_source_type_is_never_warned_about(tmp_path: Path) -> None:
    """The advisory is scoped to the source type whose ids the fix moved.

    Companion invariant to the two above, and the boundary that keeps the
    subject-fix from over-correcting into noise: #1329 changed *markdown* id
    derivation, so a document or image run — even on an un-pinned vault, even
    under a borrowed ledger — has nothing this advisory can warn it about. The
    gate is the run's own ``source_type``, which no ``ledger_source`` override
    can move.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    assert unpinned_vault_warning("document", vault) is None
    assert unpinned_vault_warning("discord", vault) is None


# ---- (b) + (c) the migration's core promise ----


def test_pinning_makes_the_next_reingest_a_no_op_under_the_original_id(
    tmp_path: Path,
) -> None:
    """After the migration the same re-ingest reports ``unchanged`` and moves nothing.

    The id assertion is the point. A migration that merely stopped the
    duplicate by re-minting would satisfy the file count and still break every
    resonance edge, embedding-cache row and child id that references the old
    value.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    result = pin_source_ids(vault)
    assert result.pinned == 1
    assert result.conflicts == []
    assert result.unpinnable == []

    reingested = _reingest(vault, source.parent)

    assert reingested.unchanged == 1
    assert reingested.created == 0
    files = _fragment_files(vault)
    assert len(files) == 1
    assert str(frontmatter.load(str(files[0]))["id"]) == _PRE_FIX_ID


def test_pinning_changes_nothing_but_the_origin_key(tmp_path: Path) -> None:
    """``id``, ``created``, the body and the filename are all preserved exactly.

    Pinning is not a rewrite. ``created`` in particular feeds the fragment's
    filename through the writer's date prefix, so moving it would rename files
    — the migration must not.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    md_file = _seed_pre_fix_fragment(vault, source)

    before = frontmatter.load(str(md_file))
    assert _origin_key(md_file) is None

    pin_source_ids(vault)

    after_files = _fragment_files(vault)
    assert after_files == [md_file]
    after = frontmatter.load(str(md_file))

    # Whole-frontmatter equality, not a handful of spot checks: the claim is
    # that *nothing* moves except one nested key, so the test states exactly
    # that and would catch a field the round-trip silently dropped or coerced.
    expected_source = dict(before.metadata["source"])
    expected_source["origin_key"] = _origin_key(md_file)
    assert after.metadata == {**before.metadata, "source": expected_source}
    assert after.content == before.content


# ---- (d) the RTBF criterion ----


def test_pinning_stamps_the_origin_key_the_purge_sweep_keys_on(
    tmp_path: Path,
) -> None:
    """A pinned fragment gains ``source.origin_key`` in its on-disk frontmatter.

    The RTBF purge sweep resolves its target by reading that field off disk and
    skips any fragment without it, and ``update_fragment`` preserves on-disk
    frontmatter rather than merging fresh ``source`` fields — so a fragment
    that lacks the key at migration time would never gain one on any later
    ingest. Omitting this stamp would permanently strand purge coverage for
    exactly the population the migration protects.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    md_file = _seed_pre_fix_fragment(vault, source)

    pin_source_ids(vault)

    stamped = _origin_key(md_file)
    assert stamped is not None
    ledger = SourceLedger.load(vault, source="markdown")
    record = ledger.get(stamped)
    assert record is not None
    assert record.fragment_id == _PRE_FIX_ID


# ---- (e) + (f) dry-run and idempotency ----


def test_dry_run_reports_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` is a preview: no ledger append, no frontmatter stamp."""
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    md_file = _seed_pre_fix_fragment(vault, source)
    before = md_file.read_bytes()

    result = pin_source_ids(vault, dry_run=True)

    assert result.pinned == 1
    assert md_file.read_bytes() == before
    assert _origin_key(md_file) is None
    assert len(SourceLedger.load(vault, source="markdown")) == 0


def test_an_interrupted_run_is_repaired_by_the_next_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-pinned fragment gets its missing ``origin_key`` on the retry.

    Pinning is two durable writes — a ledger record and a frontmatter stamp —
    and they cannot be atomic with respect to each other. A failure between
    them (a disk-full or permission ``OSError`` partway through an
    N-fragment pass is far likelier than a ``kill -9``) leaves the record
    present and the stamp missing.

    Without the repair path that state is **permanent and silent**: the retry
    sees the key in the ledger, reports the fragment as already pinned, and
    never stamps it — so the RTBF purge sweep, which resolves its target from
    on-disk frontmatter and skips fragments lacking the key, can never see
    that fragment again. ``update_fragment`` does not merge fresh ``source``
    fields either, so no later ingest repairs it.

    The failure is injected on the *second* of three fragments so the test
    also pins that the pass is resumable in the middle, not just at its edges.
    """
    vault = _make_vault(tmp_path)
    fragments = []
    for index in range(3):
        source = _make_source(tmp_path, name=f"n{index}.md", body=f"Body {index}.\n")
        fragments.append(
            _seed_pre_fix_fragment(
                vault,
                source,
                fragment_id=f"frag-00000000000{index}",
                body=f"Body {index}.",
            )
        )

    real_stamp = pin_ids._stamp_origin_key
    calls = {"n": 0}

    def flaky_stamp(md_file: Path, source_key: str) -> None:
        """Fail on the second stamp, as a full disk would."""
        calls["n"] += 1
        if calls["n"] == 2:
            msg = "simulated disk full"
            raise OSError(msg)
        real_stamp(md_file, source_key)

    monkeypatch.setattr(pin_ids, "_stamp_origin_key", flaky_stamp)
    with pytest.raises(OSError, match="simulated disk full"):
        pin_source_ids(vault)
    monkeypatch.undo()

    # The failure aborted the pass, so the vault now holds three distinct
    # states: fragment 0 fully pinned, fragment 1 HALF-pinned (record written,
    # stamp lost — the dangerous one), fragment 2 never reached at all.
    ledger_after_tear = SourceLedger.load(vault, source="markdown")
    assert _origin_key(fragments[0]) is not None
    assert _origin_key(fragments[1]) is None
    assert len(ledger_after_tear) == 2, "fragment 1's record must have landed"

    result = pin_source_ids(vault)

    # Exactly one fragment was half-pinned and therefore repaired; the
    # untouched third is a normal fresh pin, not a repair.
    assert result.repaired == 1
    assert result.pinned == 1
    assert result.already_pinned == 2
    assert all(_origin_key(path) is not None for path in fragments)
    # And the repair is not a re-pin: no id moved and no record was replaced.
    ledger = SourceLedger.load(vault, source="markdown")
    for index, path in enumerate(fragments):
        assert str(frontmatter.load(str(path))["id"]) == f"frag-00000000000{index}"
    assert len(ledger) == len(fragments)


def test_a_repaired_run_reports_zero_repairs_the_third_time(
    tmp_path: Path,
) -> None:
    """Once repaired, the fragment stops being reported — the repair converges."""
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)
    pin_source_ids(vault)

    result = pin_source_ids(vault)

    assert result.repaired == 0
    assert result.already_pinned == 1


def test_a_second_run_pins_nothing(tmp_path: Path) -> None:
    """The migration is idempotent, so running it twice is safe."""
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    first = pin_source_ids(vault)
    second = pin_source_ids(vault)

    assert first.pinned == 1
    assert second.pinned == 0
    assert second.already_pinned == 1


# ---- (g) an already-duplicated vault ----


def test_a_contested_source_key_pins_neither_fragment(tmp_path: Path) -> None:
    """Two fragments claiming one source are reported, not silently arbitrated.

    This is the vault that already got duplicated — by a timezone change, or a
    laptop move — before the operator upgraded. The ledger holds one record per
    key, so blessing one fragment would orphan the other forever. Refusing, and
    naming both paths, is the only honest answer.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source, fragment_id="frag-aaaaaaaaaaaa")
    _seed_pre_fix_fragment(vault, source, fragment_id="frag-bbbbbbbbbbbb")
    assert len(_fragment_files(vault)) == 2

    result = pin_source_ids(vault)

    assert result.pinned == 0
    assert len(result.conflicts) == 1
    assert "frag-aaaaaaaaaaaa" not in str(
        SourceLedger.load(vault, source="markdown").live_keys()
    )
    assert len(SourceLedger.load(vault, source="markdown")) == 0
    assert len(_fragment_files(vault)) == 2


# ---- (h) the stored-body hash correction ----


def test_a_source_edited_after_ingest_reports_updated_not_unchanged(
    tmp_path: Path,
) -> None:
    """The backfill hashes the STORED BODY, so a post-ingest edit still applies.

    If the migration recorded the hash of the *current source file* instead,
    it would file the new hash against the old stored body. The next ingest
    would compute that same hash, take the unchanged branch, and the operator's
    edit would be swallowed permanently — nothing ever revisits an unchanged
    unit. Hashing the stored body makes the run correctly report ``updated``
    and apply the edit in place, under the pinned id.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    pin_source_ids(vault)

    edited = "An untouched note.\n\nA sentence added after ingest.\n"
    source.write_text(edited, encoding="utf-8")
    os.utime(source, (PINNED_MTIME, PINNED_MTIME))

    result = _reingest(vault, source.parent)

    assert result.updated == 1
    assert result.created == 0
    files = _fragment_files(vault)
    assert len(files) == 1
    written = frontmatter.load(str(files[0]))
    assert str(written["id"]) == _PRE_FIX_ID
    assert "added after ingest" in written.content


def test_an_edit_made_before_the_migration_is_not_swallowed(tmp_path: Path) -> None:
    """A source edited *between* its ingest and the migration still applies later.

    This is the precise hazard the stored-body hash exists for, and it is a
    different scenario from an edit made *after* pinning. Here the vault holds
    the old body while the source on disk already holds the new one. A
    migration that hashed the **current source** would file the new hash
    against the old stored body; the next ``creek ingest`` would compute that
    same hash, take the unchanged branch, and the operator's edit would be
    lost permanently — nothing ever revisits an unchanged unit.

    Hashing the stored body records what the vault actually contains, so the
    divergence is still visible and the next run reports ``updated``.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    # The edit lands BEFORE the migration runs. Written with no trailing
    # newline on purpose: ``frontmatter`` strips one on the way in, so a file
    # that ends in "\n" would hash differently from its own parsed content and
    # a source-hashing implementation would look correct here by accident.
    edited = "An untouched note.\n\nEdited before the migration ran."
    source.write_text(edited, encoding="utf-8")
    os.utime(source, (PINNED_MTIME, PINNED_MTIME))

    assert pin_source_ids(vault).pinned == 1

    result = _reingest(vault, source.parent)

    assert result.updated == 1
    assert result.unchanged == 0
    assert result.created == 0
    files = _fragment_files(vault)
    assert len(files) == 1
    written = frontmatter.load(str(files[0]))
    assert str(written["id"]) == _PRE_FIX_ID
    assert "Edited before the migration ran." in written.content


# ---- (i) sources that cannot be pinned ----


@pytest.mark.parametrize(
    ("original_file", "fragment_marker"),
    [
        pytest.param("", "no-source", id="no-original-file"),
        pytest.param("/nowhere/gone.md", "vanished", id="source-deleted"),
    ],
)
def test_an_unresolvable_source_is_reported_and_not_pinned(
    tmp_path: Path,
    original_file: str,
    fragment_marker: str,
) -> None:
    """A fragment with no live markdown source is counted and named, not guessed at.

    Pinning a vanished source would file a record no future run can match and,
    worse, could shadow a different file that later takes that path.

    Args:
        tmp_path: Pytest temp directory.
        original_file: The ``source.original_file`` to seed.
        fragment_marker: Distinguishes the parametrised fragment titles.
    """
    vault = _make_vault(tmp_path)
    fragment = Fragment(
        id=f"frag-{fragment_marker[:4]}00000000",
        title=fragment_marker,
        created=datetime(2024, 3, 14, 19, 0, tzinfo=UTC),
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            original_file=original_file or None,
        ),
    )
    md_file = VaultWriter(vault_path=vault).write_fragment(fragment, body=_BODY)

    result = pin_source_ids(vault)

    assert result.pinned == 0
    assert len(result.unpinnable) == 1
    assert str(md_file) in result.unpinnable[0]
    assert len(SourceLedger.load(vault, source="markdown")) == 0
    assert _origin_key(md_file) is None


def test_a_non_markdown_source_is_left_to_its_own_ingestor(tmp_path: Path) -> None:
    """A ``.pdf``-sourced fragment does not belong in the markdown ledger."""
    vault = _make_vault(tmp_path)
    pdf = tmp_path / "src" / "report.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    fragment = Fragment(
        id="frag-cccccccccccc",
        title="report",
        created=datetime(2024, 3, 14, 19, 0, tzinfo=UTC),
        source=FragmentSource(
            platform=SourcePlatform.DOCUMENT,
            original_file=str(pdf),
        ),
    )
    VaultWriter(vault_path=vault).write_fragment(fragment, body=_BODY)

    result = pin_source_ids(vault)

    assert result.pinned == 0
    assert len(result.unpinnable) == 1
    assert ".md" in result.unpinnable[0]


# ---- (j) a relatively-pathed source, migrated from a different directory ----
#
# ``source.original_file`` is stored verbatim as whatever path the ingest run
# was handed (``MarkdownIngestor.parse`` records ``str(raw.path)`` and the CLI
# does not resolve ``--source``), so an operator who ran ``creek ingest
# --source 00-Inbox`` from the vault root has a vault full of *relative*
# recorded paths. The one-time migration is a separate invocation and there is
# nothing to make an operator run it from the same directory.


def _make_in_vault_source(vault: Path, relpath: str = "00-Inbox/note.md") -> Path:
    """Write a pinned-mtime markdown source *inside* the vault.

    The in-vault case is what a relatively-pathed record is normally about:
    journals kept in the vault and ingested from the vault root.

    Args:
        vault: Vault root.
        relpath: Vault-relative location for the source file.

    Returns:
        The absolute path to the created source file.
    """
    src = vault / relpath
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_BODY, encoding="utf-8")
    os.utime(src, (PINNED_MTIME, PINNED_MTIME))
    return src


def test_a_relatively_pathed_source_is_pinned_from_any_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live fragment is protected even when the migration runs from elsewhere.

    Resolving the recorded path against the *current* directory rather than
    against the vault it was recorded relative to made this fragment look
    deleted, so the migration skipped it — leaving the one population the
    migration exists to protect exposed to the re-minting duplication. The
    report line said so, but an operator who runs a one-shot migration, sees a
    few sources called unpinnable and moves on has not been protected.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Used to run the migration from an unrelated directory.
    """
    vault = _make_vault(tmp_path)
    source = _make_in_vault_source(vault)
    md_file = _seed_pre_fix_fragment(vault, source.relative_to(vault))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = pin_source_ids(vault)

    assert result.unpinnable == []
    assert result.pinned == 1
    assert _origin_key(md_file) == "00-Inbox/note.md"

    # The outcome that matters: the pin actually holds against the re-ingest.
    reingested = _reingest(vault, source.parent)

    assert reingested.unchanged == 1
    assert reingested.created == 0
    files = _fragment_files(vault)
    assert len(files) == 1
    assert str(frontmatter.load(str(files[0]))["id"]) == _PRE_FIX_ID


def test_a_relatively_pathed_source_pins_the_same_key_from_either_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded ``source_key`` does not depend on where the migration ran.

    Two identical vaults, two different working directories, one key. A key
    that varied with the operator's shell would file the pin under a name the
    next ingest does not look up, which re-mints just as surely as skipping the
    fragment does.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Used to switch working directory between the two runs.
    """
    keys: list[str | None] = []
    for index, cwd_name in enumerate(("inside", "elsewhere")):
        vault = _make_vault(tmp_path / f"case{index}")
        source = _make_in_vault_source(vault)
        md_file = _seed_pre_fix_fragment(vault, source.relative_to(vault))
        cwd = vault if cwd_name == "inside" else tmp_path / f"case{index}-cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd)

        assert pin_source_ids(vault).pinned == 1
        keys.append(_origin_key(md_file))

    assert keys == ["00-Inbox/note.md", "00-Inbox/note.md"]


def test_a_relative_source_resolving_where_it_stands_still_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vault is the fallback anchor, not the preferred one.

    A migration run from the original ingest's own directory must key exactly
    as it did before this anchoring existed, or the fix would quietly re-point
    already-correct records at a same-named file that happens to sit in the
    vault. Here the recorded path resolves *both* ways: the real source is
    outside the vault under the working directory, and a decoy of the same
    vault-relative name sits inside. The working directory wins, so the key is
    the outside-the-vault bare-name form.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Used to run the migration from the ingest's directory.
    """
    vault = _make_vault(tmp_path)
    _make_in_vault_source(vault)  # the decoy, same vault-relative name
    work = tmp_path / "work"
    real = work / "00-Inbox" / "note.md"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(_BODY, encoding="utf-8")
    md_file = _seed_pre_fix_fragment(vault, real.relative_to(work))
    monkeypatch.chdir(work)

    result = pin_source_ids(vault)

    assert result.pinned == 1
    assert _origin_key(md_file) == "note.md"


def test_a_relative_source_that_exists_nowhere_is_still_unpinnable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anchoring to the vault widens what resolves — it does not pin blindly.

    The refusal in :func:`~creek.ingest.pin_ids._unpinnable_reason` is a real
    safety property: a record filed against a path holding no file can shadow a
    different file that later takes that path. A relative path that names
    nothing under the vault *or* the working directory is still a vanished
    source.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Used to run the migration from an unrelated directory.
    """
    vault = _make_vault(tmp_path)
    fragment = Fragment(
        id="frag-deadbeef0000",
        title="ghost",
        created=datetime(2024, 3, 14, 19, 0, tzinfo=UTC),
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            original_file="00-Inbox/never-existed.md",
        ),
    )
    md_file = VaultWriter(vault_path=vault).write_fragment(fragment, body=_BODY)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = pin_source_ids(vault)

    assert result.pinned == 0
    assert len(result.unpinnable) == 1
    assert str(md_file) in result.unpinnable[0]
    assert len(SourceLedger.load(vault, source="markdown")) == 0
    assert _origin_key(md_file) is None


# ---- CLI surface ----


def _run_cli(*argv: str) -> tuple[int, str]:
    """Invoke the ``creek`` CLI and return its exit code and plain-text output.

    SGR escapes are stripped so substring assertions are not defeated by
    rich's colouring if a future console gains a forced-colour mode.

    Args:
        *argv: Command-line arguments.

    Returns:
        ``(exit_code, output)`` with ANSI styling removed from *output*.
    """
    result = CliRunner().invoke(app, list(argv))
    return result.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_cli_pin_source_ids_runs_the_migration(tmp_path: Path) -> None:
    """``creek ingest --pin-source-ids`` pins the vault and reports what it did."""
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    md_file = _seed_pre_fix_fragment(vault, source)

    code, output = _run_cli("ingest", "--pin-source-ids", "--vault", str(vault))

    assert code == 0, output
    assert "Pinned 1" in output
    assert _origin_key(md_file) is not None
    assert len(SourceLedger.load(vault, source="markdown")) == 1


def test_cli_pin_source_ids_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` says so on screen and leaves the vault untouched."""
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    md_file = _seed_pre_fix_fragment(vault, source)
    before = md_file.read_bytes()

    code, output = _run_cli(
        "ingest", "--pin-source-ids", "--dry-run", "--vault", str(vault)
    )

    assert code == 0, output
    assert "Would pin 1" in output
    assert "nothing was written" in output
    assert md_file.read_bytes() == before
    assert len(SourceLedger.load(vault, source="markdown")) == 0


def test_cli_pin_source_ids_prints_every_action_item_in_full(
    tmp_path: Path,
) -> None:
    """Conflicts and unpinnable fragments are listed, never reduced to a count.

    These lines are the operator's to-do list — a summary count would tell
    them something is wrong without telling them which fragment.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source, fragment_id="frag-aaaaaaaaaaaa")
    _seed_pre_fix_fragment(vault, source, fragment_id="frag-bbbbbbbbbbbb")

    code, output = _run_cli("ingest", "--pin-source-ids", "--vault", str(vault))

    assert code == 0, output
    assert "Conflicts" in output
    assert "claimed by 2 fragments" in output


def test_cli_dry_run_is_refused_rather_than_ignored_by_other_migrations(
    tmp_path: Path,
) -> None:
    """``--dry-run --refresh-dates`` is rejected, not silently run for real.

    ``--dry-run`` is new to ``creek ingest`` and is scoped to
    ``--pin-source-ids``. Quietly running a *different* migration for real
    while the operator believes they asked for a preview is the worst
    available reading of the flag, so the combination is refused.
    """
    vault = _make_vault(tmp_path)

    code, _output = _run_cli(
        "ingest", "--dry-run", "--refresh-dates", "--vault", str(vault)
    )

    assert code != 0


def test_cli_pin_source_ids_exits_one_on_a_missing_vault(tmp_path: Path) -> None:
    """A vault path that does not exist is an error, not a silent no-op."""
    code, _output = _run_cli(
        "ingest", "--pin-source-ids", "--vault", str(tmp_path / "absent")
    )

    assert code == 1


def _ingest_via_cli(vault: Path, src_dir: Path) -> tuple[int, str]:
    """Run a bare ``creek ingest`` the way an operator does, through the CLI.

    Every other un-pinned-vault assertion in this module calls
    :func:`~creek.ingest.pipeline.run_ingest` directly, which is precisely how
    the advisory could be produced, stored, and still never reach a human.

    Args:
        vault: Vault root.
        src_dir: Directory holding the source files.

    Returns:
        ``(exit_code, output)`` with ANSI styling removed.
    """
    return _run_cli(
        "ingest",
        "--type",
        "markdown",
        "--input",
        str(src_dir),
        "--vault",
        str(vault),
        "--yes",
    )


def test_cli_ingest_prints_the_unpinned_vault_advisory_before_it_writes(
    tmp_path: Path,
) -> None:
    """A plain ``creek ingest`` shows the advisory, ahead of its own write pass.

    This is the whole safety mechanism: an advisory the operator never sees is
    not an advisory. It is asserted on rendered output rather than on
    ``IngestRunResult.warnings`` because the gap being guarded is exactly the
    step between the two.

    Ordering is part of the contract, not incidental. The advisory is emitted
    at detection time — before the first fragment is written — so the operator
    who is watching can still abort with nothing yet duplicated. Printed after
    the run it would only ever be an explanation of damage already done.

    The remedy command is asserted whole: it is meant to be copy-pasted, and a
    line-wrap through the middle of it is a broken instruction.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)

    code, output = _ingest_via_cli(vault, source.parent)

    assert code == 0, output
    assert "creek ingest --pin-source-ids --vault <vault>" in output
    assert output.index("--pin-source-ids") < output.index("Ingest summary:")


def test_cli_ingest_stays_quiet_about_a_pinned_vault(tmp_path: Path) -> None:
    """A migrated vault gets no advisory, so the warning keeps its meaning.

    Without this, printing the advisory unconditionally would pass the
    companion test while training the operator to ignore it.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _seed_pre_fix_fragment(vault, source)
    pin_source_ids(vault)

    code, output = _ingest_via_cli(vault, source.parent)

    assert code == 0, output
    assert "--pin-source-ids" not in output


def test_the_reproduction_diagnostic_counts_a_reproducing_fragment(
    tmp_path: Path,
) -> None:
    """A fragment that re-derives its own id is counted, and pinned regardless.

    The diagnostic is advisory. It is reported so an operator can see how much
    of the vault independently corroborates its own pinning, and it gates
    nothing — a migration conditioned on reproduction would skip exactly the
    fragments most at risk.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    created = datetime(2024, 3, 15, 9, 0, tzinfo=UTC)
    # The body the diagnostic re-hashes is the one the vault *stores*, and
    # ``frontmatter`` strips the trailing newline on the round-trip. Hashing
    # the pre-write string would silently never reproduce, which is exactly
    # the accident this test exists to rule out.
    stored_body = _BODY.rstrip("\n")
    reproducing_id = generate_fragment_id(str(source), created, stored_body)
    fragment = Fragment(
        id=reproducing_id,
        title=source.stem,
        created=created,
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            original_file=str(source),
        ),
    )
    VaultWriter(vault_path=vault).write_fragment(fragment, body=stored_body)

    result = pin_source_ids(vault)

    assert result.reproduced == 1
    assert result.pinned == 1
    assert result.examined == 1
