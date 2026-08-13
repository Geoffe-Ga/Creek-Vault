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
    from collections.abc import Iterator

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


def test_the_sweep_unlinks_a_symlink_to_a_file_but_never_follows_one_to_a_dir(
    tmp_path: Path,
) -> None:
    """The meta sweep copies the content wipe's symlink policy exactly.

    Two halves, and they disagree on purpose. A symlink *to a file* is
    destroyed, because ``unlink`` really does destroy that alias and
    leaving it would leave a live pointer into content the erasure was
    supposed to remove. A symlink *to a directory* is neither followed
    nor unlinked: the files behind it are not this purge's to claim, and
    walking through one is how an erasure of a vault comes to delete
    somebody's home directory.
    """
    from creek.purge.meta import sweep_unkept_meta

    meta = tmp_path / "00-Creek-Meta"
    meta.mkdir()
    outside = tmp_path / "outside"
    (outside / "private").mkdir(parents=True)
    (outside / "private" / "secret.md").write_text("not ours\n", encoding="utf-8")
    (outside / "loose.md").write_text("aliased\n", encoding="utf-8")

    (meta / "link-to-file.md").symlink_to(outside / "loose.md")
    (meta / "link-to-dir").symlink_to(outside / "private", target_is_directory=True)

    removed: list[Path] = []
    count = sweep_unkept_meta(
        meta,
        skip=lambda _path: False,
        remove=removed.append,
    )

    assert count == 1
    assert removed == [meta / "link-to-file.md"]
    assert (outside / "private" / "secret.md").exists()


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
