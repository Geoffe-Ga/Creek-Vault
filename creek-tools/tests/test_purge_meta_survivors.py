"""Pin the documented ``00-Creek-Meta/`` survivor set against the code (#1453).

``creek purge vault`` sweeps ``00-Creek-Meta/`` deny-by-default: every
regular file is destroyed unless :data:`creek.purge.meta.META_PURGE_KEEP`
shelters it. That inversion is only as good as its keep-list, and a
keep-list is exactly the kind of thing that grows a quiet entry during an
unrelated change. So the list is pinned three ways here:

* **against the docs** — the table in ``docs/cleaning-and-purge.md`` and
  the tuple in ``creek/purge/meta.py`` must name the same paths, so
  neither can move without the other;
* **against a real purge** — a vault is seeded with one file at every
  path the table names, purged, and the survivors compared with ``==``.
  Not ``<=``: a subset assertion passes on a sweep that keeps something
  extra, which is the only failure mode that matters here;
* **against itself** — :func:`test_the_pin_notices_an_undocumented_keep`
  adds an entry to the keep-list and asserts the pin goes red, because a
  guard that cannot fail is not a guard.

The fixture is seeded from an **explicit literal list** parsed out of the
documentation, never from an ``rglob`` of the vault. A discovery-based
fixture cannot tell an artifact it seeded from one the purge itself wrote
during the run — and this repository's own checkout contains a real
``creek-tools/00-Creek-Meta/`` directory for such a walk to wander into.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

import pytest

from creek.purge import PurgeAuditEntry, PurgeAuditLog, PurgeEngine
from creek.purge.engine import VAULT_PURGE_CONFIRMATION
from creek.purge.meta import META_PURGE_KEEP, SWEEP_EXEMPT, MetaKeep

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

DOC_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "docs" / "cleaning-and-purge.md"
)
"""The document whose table this module holds to the code."""

_TABLE_BEGIN: Final[str] = "<!-- META-SURVIVOR-TABLE:BEGIN -->"
_TABLE_END: Final[str] = "<!-- META-SURVIVOR-TABLE:END -->"

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(keep|wipe|exempt)\s*\|\s*(.+?)\s*\|$")
"""One table row: a backticked path, a disposition, and a reason."""


class DocRow(NamedTuple):
    """One parsed row of the documented survivor table.

    Attributes:
        relpath: Path relative to ``00-Creek-Meta/``. A trailing slash
            marks a directory whose whole subtree shares the
            disposition.
        disposition: ``keep``, ``wipe`` or ``exempt``.
        reason: The prose justification, required to be non-empty.
    """

    relpath: str
    disposition: str
    reason: str


def _parse_doc_table() -> tuple[DocRow, ...]:
    """Parse the survivor table out of ``docs/cleaning-and-purge.md``.

    Returns:
        Every row, in document order.

    Raises:
        AssertionError: If the delimited block is missing, or holds a
            line that is neither a row nor the header.
    """
    text = DOC_PATH.read_text(encoding="utf-8")
    assert _TABLE_BEGIN in text, f"{DOC_PATH} lost its survivor-table marker"
    body = text.split(_TABLE_BEGIN, 1)[1].split(_TABLE_END, 1)[0]
    rows: list[DocRow] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("| -"):
            continue
        match = _ROW.match(stripped)
        if match is None:
            assert "Disposition" in stripped, f"Unparseable table row: {stripped}"
            continue
        rows.append(DocRow(match.group(1), match.group(2), match.group(3)))
    assert rows, "The survivor table parsed to zero rows"
    return tuple(rows)


DOC_ROWS: Final[tuple[DocRow, ...]] = _parse_doc_table()


def _paths_with(disposition: str) -> set[str]:
    """Return the documented paths carrying *disposition*, slashes stripped.

    Args:
        disposition: ``keep``, ``wipe`` or ``exempt``.

    Returns:
        Vault-relative paths under ``00-Creek-Meta/``.
    """
    return {
        row.relpath.rstrip("/") for row in DOC_ROWS if row.disposition == disposition
    }


def _seed_relpath(row: DocRow) -> str:
    """Return a concrete file to seed for *row*.

    A directory row is seeded with the ``.gitkeep`` marker ``creek init``
    really deploys into it, so the fixture matches a live vault rather
    than a convenient invention.

    Args:
        row: One parsed table row.

    Returns:
        A path relative to ``00-Creek-Meta/`` naming a regular file.
    """
    return f"{row.relpath}.gitkeep" if row.relpath.endswith("/") else row.relpath


def _seed_documented_vault(tmp_path: Path) -> Path:
    """Build a vault holding one file at every path the table names.

    The ordering is load-bearing. ``audit/purge.jsonl`` is made valid and
    non-empty *before* the legacy ``Processing-Log/purge-log.json`` is
    written, which is what a vault that has purged before looks like: the
    legacy migration sees a populated new log, warns, and skips — leaving
    the legacy file on disk for the sweep to meet. Seed them the other way
    round and the migration consumes and unlinks the legacy file during
    setup, and the test would never exercise the keep at all.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# vault marker (GAP-003)\nredaction:\n  enabled: true\n",
        encoding="utf-8",
    )
    for folder in ("01-Fragments", "02-Threads", "03-Eddies"):
        (vault / folder).mkdir()

    PurgeAuditLog(vault).append(
        PurgeAuditEntry(operation="fragment", criteria={"fragment_id": "frag-prior"}),
    )

    for row in DOC_ROWS:
        target = vault / "00-Creek-Meta" / _seed_relpath(row)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".parquet":
            _seed_real_parquet(vault)
            continue
        target.write_text(f"seeded {row.relpath}\n", encoding="utf-8")
    return vault


def _seed_real_parquet(vault: Path) -> None:
    """Write a genuine embeddings cache, not a text file with that name.

    The exempt row has to be seeded through the real writer. Seeding it
    with placeholder text makes ``_delete_cache_file`` raise an
    unhandled ``ArrowInvalid`` out of ``pq.read_metadata`` and abort the
    whole purge — a separate defect, tracked as #1480, which this fixture
    must not trip over while testing something else.

    Args:
        vault: Vault root.
    """
    from datetime import UTC, datetime

    from creek.config import EmbeddingsConfig
    from creek.link.embeddings import (
        CachedEmbedding,
        EmbeddingLinker,
        embeddings_cache_path,
    )

    linker = EmbeddingLinker(config=EmbeddingsConfig())
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(
        {
            "frag-prior": CachedEmbedding(
                fragment_id="frag-prior",
                content_hash="0" * 64,
                model_name=linker.config.model,
                vector=[0.1, 0.2, 0.3],
                computed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        },
        cache_path,
    )


def _survivors(vault: Path) -> set[str]:
    """Return every regular file left under ``00-Creek-Meta/``.

    Args:
        vault: Vault root.

    Returns:
        Slash-joined paths relative to ``00-Creek-Meta/``.
    """
    meta = vault / "00-Creek-Meta"
    return {
        path.relative_to(meta).as_posix() for path in meta.rglob("*") if path.is_file()
    }


def _expected_survivors() -> set[str]:
    """Return the seeded files the table says should survive.

    Returns:
        Slash-joined paths relative to ``00-Creek-Meta/``.
    """
    return {_seed_relpath(row) for row in DOC_ROWS if row.disposition == "keep"}


# ---------------------------------------------------------------------------
# The keep-list cannot pass vacuously
# ---------------------------------------------------------------------------


def test_every_keep_list_entry_is_distinct_and_states_a_reason() -> None:
    """A keep-list entry without a reason is an accident, not a decision.

    Duplicates matter for the same reason: two rows for one path means
    two people decided it independently and neither saw the other, and
    the survivor table would then disagree with the tuple's length while
    naming the same set.
    """
    relpaths = [keep.relpath for keep in META_PURGE_KEEP]

    assert len(relpaths) == len(set(relpaths))
    assert all(keep.reason.strip() for keep in META_PURGE_KEEP)


def test_no_keep_list_entry_restates_the_meta_prefix() -> None:
    """Entries are relative to ``00-Creek-Meta/`` and never absolute.

    An entry spelled ``00-Creek-Meta/audit/purge.jsonl`` would never
    match anything the walk produces — the walk yields paths already
    relative to that directory — so it would shelter nothing while
    looking exactly like it did.
    """
    for keep in META_PURGE_KEEP:
        assert not keep.relpath.is_absolute()
        assert keep.relpath.parts[0] != "00-Creek-Meta"


def test_the_keep_list_validator_rejects_a_reasonless_entry() -> None:
    """The constructor-time check is real, not decorative."""
    from pathlib import PurePosixPath

    from creek.purge.meta import _validate_keep_list

    with pytest.raises(ValueError, match="states no reason"):
        _validate_keep_list((MetaKeep(PurePosixPath("x.json"), "   "),))

    with pytest.raises(ValueError, match="Duplicate"):
        _validate_keep_list(
            (
                MetaKeep(PurePosixPath("x.json"), "a"),
                MetaKeep(PurePosixPath("x.json"), "b"),
            ),
        )

    with pytest.raises(ValueError, match="must be relative"):
        _validate_keep_list(
            (MetaKeep(PurePosixPath("00-Creek-Meta/x.json"), "a"),),
        )

    # The meta root itself, spelled as the empty relative path. Under
    # equality matching it shelters nothing, and under any future
    # containment mode it would shelter the whole directory this module
    # exists to sweep — so it is rejected outright rather than left to
    # mean whichever of those two the matcher happens to implement.
    with pytest.raises(ValueError, match="names no file"):
        _validate_keep_list((MetaKeep(PurePosixPath("."), "a"),))


# ---------------------------------------------------------------------------
# Doc ↔ code
# ---------------------------------------------------------------------------


def test_the_documented_keep_rows_are_exactly_the_keep_list() -> None:
    """``docs/cleaning-and-purge.md`` and ``meta.py`` name the same survivors.

    Exact equality in both directions. A keep the code has and the docs
    do not is an undocumented survivor of an erasure request; a keep the
    docs have and the code does not is a promise the sweep breaks.
    """
    assert _paths_with("keep") == {str(keep.relpath) for keep in META_PURGE_KEEP}


def test_the_documented_exempt_rows_are_exactly_the_exempt_tuple() -> None:
    """Exempt is a third disposition and is documented as such.

    ``embeddings.parquet`` is destroyed but not by this sweep, and
    filing it under "keep" would tell an operator it survives an
    erasure.
    """
    assert _paths_with("exempt") == {str(path) for path in SWEEP_EXEMPT}


def test_every_documented_row_states_a_reason() -> None:
    """Including the wipe rows: destroying a file is also a decision."""
    for row in DOC_ROWS:
        assert row.reason.strip(), f"{row.relpath} is documented without a reason"


def test_a_vault_purge_leaves_exactly_the_documented_survivors(
    tmp_path: Path,
) -> None:
    """Seed one file per documented path, purge, compare the sets with ``==``.

    The load-bearing assertion of this module. A subset check would pass
    on a sweep that quietly kept an extra artifact, which is precisely
    the regression the deny-by-default design exists to prevent.
    """
    vault = _seed_documented_vault(tmp_path)

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert _survivors(vault) == _expected_survivors()


def test_the_sweep_cannot_weaken_the_privacy_posture(tmp_path: Path) -> None:
    """Neither the vault marker nor the privacy record is loosened by a purge.

    Two separate invariants, both easy to break with a shorter keep-list.

    ``creek_config.yaml`` survives byte-identical: it is the GAP-003
    marker, and a sweep that took it would leave a directory ``creek``
    no longer treats as a vault — including for the *next* purge, whose
    marker check would then refuse. It also holds the operator's
    redaction and classification configuration, so recreating it from
    defaults would silently relax both.

    ``audit/privacy.jsonl`` survives because it is the record of
    privacy-tier overrides. The ``privacy_tier`` ratchet itself is
    per-fragment frontmatter, not configuration, so a whole-vault purge
    destroys it along with the fragments — this log is the only thing
    left that says a tier was ever raised.
    """
    vault = _seed_documented_vault(tmp_path)
    config = vault / "00-Creek-Meta" / "creek_config.yaml"
    before = config.read_bytes()

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert config.read_bytes() == before
    assert (vault / "00-Creek-Meta" / "audit" / "privacy.jsonl").exists()


def test_the_pin_notices_an_undocumented_keep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a keep the docs do not list turns the survivor pin red.

    The falsifiability proof. Without it, every assertion above could be
    passing because the sweep destroys everything it is ever shown, and
    nobody would know until a real keep was added silently.
    """
    from pathlib import PurePosixPath

    import creek.purge.meta as meta_module

    monkeypatch.setattr(
        meta_module,
        "META_PURGE_KEEP",
        (*META_PURGE_KEEP, MetaKeep(PurePosixPath("dedup-manifest.json"), "smuggled")),
    )
    vault = _seed_documented_vault(tmp_path)

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    survivors = _survivors(vault)
    assert "dedup-manifest.json" in survivors
    assert survivors != _expected_survivors()


def test_the_sweep_is_what_destroys_the_documented_wipe_rows(
    tmp_path: Path,
) -> None:
    """Every ``wipe`` row is really gone, named one at a time.

    The set equality above would also pass if a wipe row had never been
    seeded. Asserting per-path absence against the seeded file makes
    that impossible.
    """
    vault = _seed_documented_vault(tmp_path)

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    for row in DOC_ROWS:
        if row.disposition != "wipe":
            continue
        target = vault / "00-Creek-Meta" / _seed_relpath(row)
        assert not target.exists(), f"{row.relpath} survived a whole-vault purge"


def _documented_dispositions() -> Iterator[str]:
    """Yield each row's disposition, for the coverage assertion below.

    Yields:
        ``keep``, ``wipe`` or ``exempt``.
    """
    for row in DOC_ROWS:
        yield row.disposition


def test_the_table_covers_all_three_dispositions() -> None:
    """A table that lost its wipe rows would still satisfy the pins above."""
    assert set(_documented_dispositions()) == {"keep", "wipe", "exempt"}


# ---------------------------------------------------------------------------
# The walk's own edges
# ---------------------------------------------------------------------------


def test_the_sweep_unlinks_a_symlink_to_a_file_or_to_nothing_but_never_to_a_dir(
    tmp_path: Path,
) -> None:
    """The meta sweep's whole symlink table, one case per link.

    Three dispositions, and they disagree on purpose.

    * A symlink **to a file** is destroyed: ``unlink`` really does
      destroy that alias, and leaving it would leave a live pointer into
      content the erasure was supposed to remove.
    * A symlink **to a directory** is neither followed nor unlinked: the
      files behind it are not this purge's to claim, and walking through
      one is how an erasure of a vault comes to delete somebody's home
      directory.
    * A **dangling** symlink is destroyed (#1485). ``Path.is_file()``
      follows the link and answers ``False`` for a dangling one, so the
      pre-#1485 walk dropped it on the floor — and the residue is not
      the body, it is the *target string*, which in this vault is
      routinely title-derived.

    None of the three touches anything outside the meta root: both
    out-of-vault targets are asserted intact afterwards, and the
    dangling link's target is asserted never to have been created.
    """
    from creek.purge.meta import sweep_unkept_meta

    meta = tmp_path / "00-Creek-Meta"
    meta.mkdir()
    outside = tmp_path / "outside"
    (outside / "private").mkdir(parents=True)
    (outside / "private" / "secret.md").write_text("not ours\n", encoding="utf-8")
    (outside / "loose.md").write_text("aliased\n", encoding="utf-8")
    never_created = outside / "private" / f"{_PRIVATE}.md"

    (meta / "link-to-file.md").symlink_to(outside / "loose.md")
    (meta / "link-to-dir").symlink_to(outside / "private", target_is_directory=True)
    (meta / "link-to-nothing.md").symlink_to(never_created)

    removed: list[Path] = []
    count = sweep_unkept_meta(
        meta,
        skip=lambda _path: False,
        remove=removed.append,
    )

    assert count == 2
    assert removed == [meta / "link-to-file.md", meta / "link-to-nothing.md"]
    assert (outside / "private" / "secret.md").exists()
    assert (outside / "loose.md").exists()
    assert not never_created.exists()


def test_a_symlink_standing_at_a_kept_path_is_still_sheltered(
    tmp_path: Path,
) -> None:
    """Only a *directory* defeats a shelter, not a link (#1484).

    The #1484 policy is narrow on purpose: a keep or exempt entry stops
    sheltering when a **directory** stands at its path, because only a
    directory turns one documented survivor into an unbounded number of
    undocumented ones. A symlink is a single alias, so it is sheltered
    exactly as a regular file at that path would be, and the sweep does
    not follow it to find out what it points at.
    """
    from creek.purge.meta import sweep_unkept_meta

    meta = tmp_path / "00-Creek-Meta"
    (meta / "audit").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_log = outside / "purge.jsonl"
    real_log.write_text("{}\n", encoding="utf-8")
    (meta / "audit" / "purge.jsonl").symlink_to(real_log)
    (meta / "dedup-manifest.json").write_text("{}\n", encoding="utf-8")

    removed: list[Path] = []
    count = sweep_unkept_meta(
        meta,
        skip=lambda _path: False,
        remove=removed.append,
    )

    assert count == 1
    assert removed == [meta / "dedup-manifest.json"]
    assert (meta / "audit" / "purge.jsonl").is_symlink()
    assert real_log.exists()


def test_the_sweep_is_a_no_op_on_a_vault_with_no_meta_directory(
    tmp_path: Path,
) -> None:
    """A vault missing ``00-Creek-Meta/`` sweeps nothing and does not raise."""
    from creek.purge.meta import sweep_unkept_meta

    def _never(_path: Path) -> None:
        """Fail loudly if the sweep tries to remove anything.

        Args:
            _path: Unused.

        Raises:
            AssertionError: Always; reaching it is the failure.
        """
        raise AssertionError("nothing to remove")

    assert (
        sweep_unkept_meta(
            tmp_path / "absent",
            skip=lambda _path: False,
            remove=_never,
        )
        == 0
    )


def test_the_skip_predicate_suppresses_the_count_as_well_as_the_removal(
    tmp_path: Path,
) -> None:
    """A skipped file is not counted — that is the whole point of the hook.

    The dry-run parity story depends on it: ``_wipe_adepthood_staging``
    walks two directories inside the sweep root and, in a preview, marks
    what it only pretended to unlink. If the sweep counted those anyway
    the preview would over-report against its own apply twin.
    """
    from creek.purge.meta import sweep_unkept_meta

    meta = tmp_path / "00-Creek-Meta"
    (meta / "adepthood" / "journal").mkdir(parents=True)
    already = meta / "adepthood" / "journal" / "entry.md"
    already.write_text("counted by an earlier pass\n", encoding="utf-8")
    fresh = meta / "dedup-manifest.json"
    fresh.write_text("{}\n", encoding="utf-8")

    removed: list[Path] = []
    count = sweep_unkept_meta(
        meta,
        skip=lambda path: path == already,
        remove=removed.append,
    )

    assert count == 1
    assert removed == [fresh]


# ---------------------------------------------------------------------------
# A keep or exempt entry shelters a file, never a subtree (#1484)
# ---------------------------------------------------------------------------

_PRIVATE: Final[str] = "2026-03-11 therapy session"
"""A recognisable title-derived string. Residue anywhere in the vault is a leak.

Titles are the point. A purge that leaves a body behind is obviously
broken; a purge that leaves a *name* behind looks clean and is not, which
is why every assertion below searches filenames and symlink targets as
well as file contents.
"""

_POLICY_BEGIN: Final[str] = "<!-- META-SURVIVOR-POLICY:BEGIN -->"
_POLICY_END: Final[str] = "<!-- META-SURVIVOR-POLICY:END -->"


def _minimal_vault(tmp_path: Path) -> Path:
    """Build the smallest directory ``purge_vault`` accepts as a vault.

    :func:`_seed_documented_vault` is deliberately not reused: it seeds a
    real ``embeddings.parquet``, and these tests need to put a
    **directory** at that path.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# vault marker (GAP-003)\n",
        encoding="utf-8",
    )
    (vault / "01-Fragments").mkdir()
    return vault


def _residue(vault: Path) -> str:
    """Return every name, body and link target under *vault*, concatenated.

    A dangling symlink's **target string** is residue too — that is the
    whole of #1485 — so links are read with ``readlink`` rather than by
    following them, which for a dangling link would read nothing at all
    and make the assertion vacuous.

    Args:
        vault: Vault root.

    Returns:
        One string to search for a private marker.
    """
    parts: list[str] = []
    for path in vault.rglob("*"):
        parts.append(path.name)
        if path.is_symlink():
            parts.append(str(path.readlink()))
        elif path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _last_outcome(vault: Path) -> dict[str, object]:
    """Return the final entry written to the preserved purge audit log.

    Args:
        vault: Vault root.

    Returns:
        The parsed last JSON line of ``00-Creek-Meta/audit/purge.jsonl``.
    """
    import json

    log = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "the purge wrote no audit entry at all"
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    return parsed


def test_a_directory_at_the_exempt_path_is_swept_not_sheltered(
    tmp_path: Path,
) -> None:
    """``embeddings.parquet/`` as a directory shelters nothing (#1484).

    The exempt tuple names one file another pass owns destroying. Before
    this fix the keep/exempt decision was taken **before** the
    ``is_dir()`` branch, so a directory standing at that path was skipped
    whole and the walk never descended: every file beneath it survived a
    whole-vault erasure while the survivor table still claimed the vault
    was clean.

    The purge then aborts — after the sweep, inside ``_delete_cache_file``,
    because ``pq.read_metadata`` cannot open a directory. That is #1480,
    and the ordering it forced (sweep first, cache delete second) is
    exactly why the erasure still happens here.
    """
    vault = _minimal_vault(tmp_path)
    leak = vault / "00-Creek-Meta" / "embeddings.parquet" / "State" / "ingest.jsonl"
    leak.parent.mkdir(parents=True)
    leak.write_text(f"{_PRIVATE}\n", encoding="utf-8")
    assert _PRIVATE in _residue(vault)

    with pytest.raises(OSError, match="directory"):
        PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert not leak.exists()
    assert _PRIVATE not in _residue(vault)


def test_a_directory_at_a_keep_path_is_swept_and_previewed_identically(
    tmp_path: Path,
) -> None:
    """A keep shelters one *file*; a directory at that path is swept (#1484).

    ``audit/redact.jsonl`` is a keep entry that no other pass of
    ``purge vault`` reads or writes, so it can stand as a directory
    without disturbing anything else. Two files, one of them nested, so
    the assertion proves the walk **descends** rather than merely
    yielding the top entry — and, because ``is_kept`` used containment
    matching, the nested one is the case a mere reordering of the
    branches would still have missed.

    Dry-run and apply are compared because a sweep that previews a
    different number from the one it performs is the failure mode this
    module exists to prevent (#1484 AC4).
    """
    vault = _minimal_vault(tmp_path)
    kept = vault / "00-Creek-Meta" / "audit" / "redact.jsonl"
    (kept / "nested").mkdir(parents=True)
    (kept / "top.jsonl").write_text(f"{_PRIVATE} A\n", encoding="utf-8")
    (kept / "nested" / "deep.jsonl").write_text(f"{_PRIVATE} B\n", encoding="utf-8")

    preview = PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert (kept / "top.jsonl").exists(), "a dry run destroyed something"
    assert (kept / "nested" / "deep.jsonl").exists()

    applied = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert preview.meta_artifacts_removed == 2
    assert applied.meta_artifacts_removed == preview.meta_artifacts_removed
    assert _PRIVATE not in _residue(vault)


# ---------------------------------------------------------------------------
# A dangling symlink is unlinked, never followed (#1485)
# ---------------------------------------------------------------------------


def _vault_with_a_link_to_a_fragment(tmp_path: Path) -> tuple[Path, Path]:
    """Build a vault holding a meta link into a title-named fragment.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root and the link path under ``00-Creek-Meta/``.
    """
    vault = _minimal_vault(tmp_path)
    journal = vault / "01-Fragments" / "Journal"
    journal.mkdir(parents=True)
    target = journal / f"{_PRIVATE}.md"
    target.write_text("---\nid: frag-x\n---\n\nbody\n", encoding="utf-8")
    link = vault / "00-Creek-Meta" / "latest-entry.md"
    link.symlink_to(target)
    return vault, link


def test_a_symlink_the_content_wipe_left_dangling_is_unlinked(
    tmp_path: Path,
) -> None:
    """The purge's own wipe makes the link dangle; the sweep must still take it.

    ``purge_vault`` wipes the ten content folders **before** the meta
    sweep, so a link under ``00-Creek-Meta/`` that pointed at a fragment
    is already dangling when the sweep meets it. ``Path.is_file()``
    follows the link and answers ``False`` for a dangling one, so the
    pre-#1485 walk dropped it: the link survived the erasure carrying a
    title-derived target string, and
    ``01-Fragments/Journal/2026-03-11 therapy session.md`` is itself
    private text.

    ``Path.exists()`` also follows, so a dangling link is invisible to
    it — the absence assertion goes through ``is_symlink`` or it would
    pass on a link that is still there.
    """
    vault, link = _vault_with_a_link_to_a_fragment(tmp_path)
    assert link.is_symlink()
    assert _PRIVATE in _residue(vault)

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert not link.is_symlink(), "the dangling link survived the purge"
    assert not link.exists()
    assert result.meta_artifacts_removed == 1
    assert _last_outcome(vault)["meta_artifacts_removed"] == 1
    assert _PRIVATE not in _residue(vault)


def test_the_dangling_link_preview_predicts_exactly_what_the_apply_removes(
    tmp_path: Path,
) -> None:
    """Dry-run and apply agree on the count, which is the hard half (#1485 AC3).

    The two runs meet *different filesystems*: a dry run does not wipe
    the content folders, so the link is still resolvable and the
    pre-#1485 walk counted it; the apply run wipes them first, so the
    same link is dangling and the pre-#1485 walk counted nothing. A
    preview that promises one removal and an apply that performs zero is
    precisely the divergence the meta sweep exists to rule out.
    """
    vault, link = _vault_with_a_link_to_a_fragment(tmp_path)

    preview = PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert link.is_symlink(), "a dry run unlinked something"

    applied = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert preview.meta_artifacts_removed == 1
    assert applied.meta_artifacts_removed == preview.meta_artifacts_removed


def test_a_dangling_link_out_of_the_vault_is_unlinked_without_touching_its_tree(
    tmp_path: Path,
) -> None:
    """Unlinked, never followed, and never repaired (#1485 AC2).

    Unlinking a symlink destroys the alias and nothing else, which is the
    only reason taking a dangling link is safe. Asserted from the other
    side too: the out-of-vault directory the link named still holds
    exactly the sibling it held before, and the missing target was never
    created.
    """
    vault = _minimal_vault(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    sibling = outside / "keep-me.md"
    sibling.write_text("untouched\n", encoding="utf-8")
    missing = outside / f"{_PRIVATE}.md"
    link = vault / "00-Creek-Meta" / "pointer.md"
    link.symlink_to(missing)

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert not link.is_symlink()
    assert result.meta_artifacts_removed == 1
    assert sibling.read_text(encoding="utf-8") == "untouched\n"
    assert not missing.exists()
    assert sorted(path.name for path in outside.iterdir()) == ["keep-me.md"]


# ---------------------------------------------------------------------------
# The policy is written down where an operator will read it (#1484 AC3)
# ---------------------------------------------------------------------------


def test_the_documented_policy_block_states_both_sweep_decisions() -> None:
    """The survivor table is not the whole promise; the edge policy is too.

    An operator reading only the table would conclude that
    ``audit/redact.jsonl`` survives, full stop. It survives *as a file*.
    The block pinned here is where the two edge decisions live — a
    directory at a keep path is swept, a dangling symlink is unlinked —
    and pinning it means the code cannot change its mind without the
    documentation changing with it.
    """
    text = DOC_PATH.read_text(encoding="utf-8")

    assert _POLICY_BEGIN in text, f"{DOC_PATH} lost its sweep-policy marker"
    block = text.split(_POLICY_BEGIN, 1)[1].split(_POLICY_END, 1)[0].strip()

    assert block, "the sweep-policy block is empty"
    assert "directory" in block
    assert "dangling" in block
    assert "symlink" in block


# ---------------------------------------------------------------------------
# The documented edge dispositions are executed, not merely spelled (#1545)
# ---------------------------------------------------------------------------

_EDGE_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\|\s*`([a-z-]+)`[^|]*\|\s*(swept|unlinked|left alone)\s*\|$"
)
"""One row of the policy block's edge table: a slug and a disposition."""

_DESTROYED: Final[frozenset[str]] = frozenset({"swept", "unlinked"})
"""The dispositions that mean "gone from disk after `purge vault`"."""


def _parse_policy_edges() -> tuple[tuple[str, str], ...]:
    """Parse the edge-case table out of the ``META-SURVIVOR-POLICY`` block.

    Returns:
        ``(slug, disposition)`` pairs, in document order.

    Raises:
        AssertionError: If the block or its table is missing.
    """
    text = DOC_PATH.read_text(encoding="utf-8")
    assert _POLICY_BEGIN in text, f"{DOC_PATH} lost its sweep-policy marker"
    block = text.split(_POLICY_BEGIN, 1)[1].split(_POLICY_END, 1)[0]
    edges = [
        (match.group(1), match.group(2))
        for line in block.splitlines()
        if (match := _EDGE_ROW.match(line.strip())) is not None
    ]
    assert edges, "the sweep-policy block states no edge dispositions at all"
    return tuple(edges)


POLICY_EDGES: Final[tuple[tuple[str, str], ...]] = _parse_policy_edges()
"""The documented edge dispositions, parsed once at import."""


def _present(path: Path) -> bool:
    """Report whether *path* is still on disk, dangling links included.

    ``Path.exists()`` follows a symlink and therefore answers ``False``
    for a link that is very much still there, which would make every
    "destroyed" assertion below pass vacuously.

    Args:
        path: The probe path.

    Returns:
        ``True`` when anything at all occupies *path*.
    """
    return path.is_symlink() or path.exists()


def _build_directory_at_a_shelter(root: Path) -> tuple[Path, Path]:
    """Seed a vault with a directory standing at the ``audit/redact.jsonl`` keep.

    Args:
        root: A private directory to build the vault under.

    Returns:
        The vault root and the probe whose fate is the disposition.
    """
    vault = _minimal_vault(root)
    probe = vault / "00-Creek-Meta" / "audit" / "redact.jsonl" / "leak.md"
    probe.parent.mkdir(parents=True)
    probe.write_text(f"{_PRIVATE}\n", encoding="utf-8")
    return vault, probe


def _build_link_the_purge_breaks(root: Path) -> tuple[Path, Path]:
    """Seed a vault with a meta link to a *directory* inside a content folder.

    The cell the rest of the suite misses. Every other symlink test
    either calls ``sweep_unkept_meta`` directly — where nothing has
    wiped the content folders, so the link still resolves to a live
    directory — or goes through ``purge_vault`` with a *file* target,
    which ``is_file()`` catches on both sides. Only a directory target
    behind a real ``purge_vault`` is classified as a live directory by
    the preview and as a dangling link by the apply run.

    Args:
        root: A private directory to build the vault under.

    Returns:
        The vault root and the link whose fate is the disposition.
    """
    vault = _minimal_vault(root)
    journal = vault / "01-Fragments" / "Journal"
    journal.mkdir(parents=True)
    (journal / f"{_PRIVATE}.md").write_text(
        "---\nid: frag-x\n---\n\nbody\n",
        encoding="utf-8",
    )
    probe = vault / "00-Creek-Meta" / "latest-thread"
    probe.symlink_to(journal, target_is_directory=True)
    return vault, probe


def _build_link_to_a_surviving_directory(root: Path) -> tuple[Path, Path]:
    """Seed a vault with a meta link to an out-of-vault directory.

    Args:
        root: A private directory to build the vault under.

    Returns:
        The vault root and the link whose fate is the disposition.
    """
    vault = _minimal_vault(root)
    outside = root / "elsewhere" / "notes"
    outside.mkdir(parents=True)
    (outside / "keep-me.md").write_text("untouched\n", encoding="utf-8")
    probe = vault / "00-Creek-Meta" / "external"
    probe.symlink_to(outside, target_is_directory=True)
    return vault, probe


_EDGE_BUILDERS: Final[dict[str, Callable[[Path], tuple[Path, Path]]]] = {
    "directory-at-a-shelter": _build_directory_at_a_shelter,
    "link-the-purge-breaks": _build_link_the_purge_breaks,
    "link-to-a-surviving-directory": _build_link_to_a_surviving_directory,
}
"""One executable vault per documented edge case, keyed by its doc slug."""


def test_the_policy_block_states_one_disposition_per_executable_edge() -> None:
    """Positive control on the doc-driven parametrisation below.

    Without this, deleting a table row (or renaming a slug) would empty
    the parametrisation and the disposition pin would pass by testing
    nothing at all.
    """
    assert len(POLICY_EDGES) == len(_EDGE_BUILDERS) > 0
    assert {slug for slug, _ in POLICY_EDGES} == set(_EDGE_BUILDERS)
    assert {disposition for _, disposition in POLICY_EDGES} == {
        "swept",
        "unlinked",
        "left alone",
    }, "the three edges must not collapse onto one disposition"


@pytest.mark.parametrize(("slug", "disposition"), POLICY_EDGES)
def test_each_documented_edge_gets_exactly_the_disposition_the_doc_states(
    slug: str,
    disposition: str,
    tmp_path: Path,
) -> None:
    """Execute the documented policy instead of grepping it for vocabulary.

    The previous pin asserted that the words ``directory``, ``dangling``
    and ``symlink`` appeared in the block, which the *opposite* policy
    also satisfies — the block could be rewritten to promise that a
    directory at a keep path is sheltered and nothing would go red. This
    one builds the vault each row describes, runs a real
    ``purge vault``, and holds the code's disposition against the word
    the documentation uses.

    The dry-run half is asserted in the same test on purpose: the
    documented parity claim ("``Meta artifacts removed`` in a
    ``--dry-run`` preview is the number the apply run really performs")
    is exactly what the ``link-the-purge-breaks`` row used to violate.
    """
    build = _EDGE_BUILDERS[slug]

    previewed_vault, previewed_probe = build(tmp_path / "preview")
    preview = PurgeEngine(previewed_vault, dry_run=True).purge_vault(
        VAULT_PURGE_CONFIRMATION
    )
    assert _present(previewed_probe), "a dry run destroyed something"

    applied_vault, applied_probe = build(tmp_path / "apply")
    applied = PurgeEngine(applied_vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    destroyed = disposition in _DESTROYED
    assert _present(applied_probe) is not destroyed, (
        f"{slug} is documented as {disposition!r} but the purge disagreed"
    )
    assert applied.meta_artifacts_removed == int(destroyed)
    assert preview.meta_artifacts_removed == applied.meta_artifacts_removed, (
        f"{slug}: the preview promised "
        f"{preview.meta_artifacts_removed} and the apply run performed "
        f"{applied.meta_artifacts_removed}"
    )


def test_a_meta_link_to_a_content_directory_previews_the_removal_it_performs(
    tmp_path: Path,
) -> None:
    """The mirrored #1485 divergence, pinned end to end (#1545 review).

    ``00-Creek-Meta/latest-thread -> 01-Fragments/Journal/`` is a link to
    a *directory*, and the sweep's directory exemption was written to
    consult the filesystem: ``entry.is_file() or not entry.exists()``.
    In an apply run the content wipe has already removed the target, so
    the link dangles and is unlinked. In a dry run nothing has been
    wiped, so the same link still resolves to a live directory and was
    left alone — **preview 0, apply 1**, the exact dry-run/apply
    divergence this PR exists to close, read in a mirror.

    The link's own name is innocuous; its *target string* is not, and
    that is the residue the RTBF request is about.
    """
    previewed_vault, previewed_link = _build_link_the_purge_breaks(tmp_path / "preview")
    preview = PurgeEngine(previewed_vault, dry_run=True).purge_vault(
        VAULT_PURGE_CONFIRMATION
    )

    assert previewed_link.is_symlink(), "a dry run unlinked something"
    assert preview.meta_artifacts_removed == 1

    applied_vault, applied_link = _build_link_the_purge_breaks(tmp_path / "apply")
    applied = PurgeEngine(applied_vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert not applied_link.is_symlink(), "the broken directory link survived"
    assert applied.meta_artifacts_removed == 1
    assert _last_outcome(applied_vault)["meta_artifacts_removed"] == 1
    assert _PRIVATE not in _residue(applied_vault)


def test_a_meta_link_to_a_content_folder_itself_is_left_alone(
    tmp_path: Path,
) -> None:
    """The wipe empties the ten content folders; it does not remove them.

    So ``00-Creek-Meta/all-fragments -> 01-Fragments`` still resolves to
    a live directory after the purge, and it is a
    ``link-to-a-surviving-directory`` rather than a
    ``link-the-purge-breaks``. The distinction is the ``!=`` in the
    engine's "is this target about to be destroyed" predicate:
    containment alone answers ``True`` for the folder root as well as
    for everything inside it, and would unlink an alias whose target is
    still standing — widening a right-to-be-forgotten sweep past what
    this document says it does. Unlike the divergence this test's
    neighbours pin, the widened form is *consistent* between preview and
    apply, so only an explicit assertion on the disposition catches it.
    """
    vault = _minimal_vault(tmp_path)
    (vault / "01-Fragments" / f"{_PRIVATE}.md").write_text(
        "---\nid: frag-x\n---\n\nbody\n",
        encoding="utf-8",
    )
    link = vault / "00-Creek-Meta" / "all-fragments"
    link.symlink_to(vault / "01-Fragments", target_is_directory=True)

    preview = PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)
    applied = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert link.is_symlink(), "a link to a surviving directory was unlinked"
    assert (vault / "01-Fragments").is_dir()
    assert applied.meta_artifacts_removed == 0
    assert preview.meta_artifacts_removed == applied.meta_artifacts_removed
    assert _PRIVATE not in _residue(vault)
