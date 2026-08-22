"""#1444: an unenumerable source subtree must not orphan the whole corpus.

---------------------------------------------------------------------------
The defect
---------------------------------------------------------------------------
``Ingestor._discover_safe`` (``creek/ingest/base.py``) swallows *every*
exception out of ``discover()`` and returns ``[]``. ``run_ingest`` then hands
that empty harvest to ``tomb_missing_units`` as ``seen_keys``, and a
full-source pass that saw no keys soft-tombs **every** live ledger key into
``10-Liminal/Orphaned/``. So one unreadable file — or one subdirectory the
process cannot enumerate — silently orphans an entire markdown corpus, and
``creek ingest`` exits 0.

The subtree shape is worse than the single-file shape.
``MarkdownIngestor._read_directory`` walks with
``sorted(dir_path.rglob("*.md"))``, and ``rglob`` *swallows* a ``scandir``
permission failure: an unreadable subdirectory simply looks empty. Measured
at HEAD over a three-entry corpus, that shape yields ``errors == []``,
``warnings == []`` and ``tombed == 3``. Completely silent.

---------------------------------------------------------------------------
Why every test here asserts on the VAULT
---------------------------------------------------------------------------
A test that inspects only ``result.errors`` is GREEN at HEAD for the subtree
shape while the corpus is being buried, because that shape reports nothing
at all. The vault is the witness that cannot be fooled: either the fragments
are still live under ``01-Fragments`` and ``10-Liminal/Orphaned`` is empty,
or the corpus was destroyed. Each test also opens with a control ingest
asserting ``written == 3``, so a fixture the ingestor never picked up cannot
satisfy "nothing was orphaned" vacuously.

---------------------------------------------------------------------------
The property under test
---------------------------------------------------------------------------
Tombing is a soft-delete primitive driven by *absence*. Absence only means
anything when the enumeration that failed to see a key was complete. So an
incomplete discovery must (a) disarm the sweep, (b) still ingest every file
it *could* read — a bare re-raise would turn one bad directory into a total
ingest outage — and (c) say so out loud on both the operator channel and the
ceiling-safe channel, with the ceiling-safe rendering withholding the
operator's filesystem layout (#1372).

Mode bits are the provocation, so every test is skipped for root and for
Windows, both of which read a 0o000 path anyway. Every chmod is reversed
unconditionally: a 0o000 directory left behind makes ``tmp_path`` teardown
raise and poisons later runs.
"""

from __future__ import annotations

import os
import stat
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.pipeline import run_ingest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from creek.ingest.pipeline import IngestRunResult

requires_mode_bits = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="root and Windows ignore mode bits, so an unreadable path stays readable",
)
"""Skip marker for tests whose provocation is a chmod that root would ignore."""

_CORPUS_MARKER = "IRREPLACEABLE-CORPUS-1444-d40a"
"""Marker carried by every entry, so a vault hit is provably this fixture."""

_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("morning-pages.md", "Morning Pages", "The creek ran high before dawn."),
    ("field-notes.md", "Field Notes", "Three herons stood on the gravel bar."),
    ("river-log.md", "River Log", "The gauge held at four feet all evening."),
)
"""Filename, title and body of the three distinct entries in every fixture."""


# ---------------------------------------------------------------------------
# Fixtures and helpers (deliberately local: no cross-imports between test
# modules, so a later edit to a neighbour cannot silently rewrite these).
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree ``run_ingest`` writes into.

    Args:
        tmp_path: Pytest-provided temporary directory to build under.

    Returns:
        Path to the scaffolded vault root.
    """
    vault = tmp_path / "vault"
    for folder in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "10-Liminal/Orphaned",
        "personal/journal",
    ):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    return vault


def _write_corpus(dir_path: Path) -> None:
    """Write the three markdown entries of :data:`_ENTRIES` into *dir_path*.

    Args:
        dir_path: Directory to create and populate.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, title, body in _ENTRIES:
        (dir_path / name).write_text(
            f"# {title}\n\n{_CORPUS_MARKER}\n\n{body}\n",
            encoding="utf-8",
        )


def _vault_fragments(vault: Path) -> list[Path]:
    """Return the live fragment files under ``01-Fragments``.

    Args:
        vault: Vault root to read.

    Returns:
        Sorted list of live fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _vault_orphans(vault: Path) -> list[Path]:
    """Return the soft-tombed fragment files under ``10-Liminal/Orphaned``.

    Args:
        vault: Vault root to read.

    Returns:
        Sorted list of tombstone paths.
    """
    return sorted((vault / "10-Liminal" / "Orphaned").rglob("*.md"))


def _ingest(vault: Path, target: Path) -> IngestRunResult:
    """Run the markdown ingestor over *target* as a full-source pass.

    ``run_ingest`` is called directly rather than through ``creek.cli``'s
    ``_run_ingest`` wrapper, which returns only ``(written, errors,
    discovered)`` — these tests have to read ``.tombed``.

    Args:
        vault: Vault root to write into.
        target: Source directory (or file) to ingest.

    Returns:
        The structured :class:`~creek.ingest.pipeline.IngestRunResult`.
    """
    return run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=target,
        vault_path=vault,
    )


def _control_pass(vault: Path, src: Path) -> None:
    """Ingest *src* cleanly and assert all three entries landed.

    Without this the later "nothing was orphaned" assertions are vacuous:
    an empty vault has no fragments to orphan.

    Args:
        vault: Vault root to write into.
        src: Source directory to ingest.
    """
    first = _ingest(vault, src)
    assert first.written == 3, (
        "the control ingest did not write all three fragments, so every "
        "assertion below would be vacuous.\n\n"
        f"written={first.written} discovered={first.discovered} "
        f"errors={first.errors}"
    )


@contextmanager
def _unreadable(path: Path) -> Iterator[None]:
    """Make *path* unreadable for the duration of the block, then restore it.

    The restore runs in a ``finally`` so it happens even when the body
    raises. This is not hygiene: a 0o000 directory left under ``tmp_path``
    makes pytest's own teardown raise, turning the result into an ERROR and
    breaking later runs and ``git clean``.

    Args:
        path: File or directory to strip all mode bits from.

    Yields:
        ``None``, with *path* unreadable.
    """
    original = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0o000)
    try:
        yield
    finally:
        path.chmod(original)


# ---------------------------------------------------------------------------
# The corpus must survive a discovery that could not see all of it
# ---------------------------------------------------------------------------


@requires_mode_bits
def test_an_unreadable_entry_does_not_orphan_the_corpus(tmp_path: Path) -> None:
    """One unreadable entry must not bury the entries beside it.

    ``read_bytes()`` on the 0o000 file raises out of ``discover()``,
    ``_discover_safe`` collects it and returns ``[]``, ``seen_keys`` is
    empty, and the tomb sweep orphans all three. The two readable entries
    were never absent; nothing about them changed.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    _write_corpus(src)
    _control_pass(vault, src)

    with _unreadable(src / _ENTRIES[1][0]):
        result = _ingest(vault, src)

    assert len(_vault_fragments(vault)) == 3, (
        "one unreadable entry emptied the live fragment set: the two "
        "readable entries were still present on disk and were orphaned "
        "anyway.\n\n"
        f"live={_vault_fragments(vault)}\norphans={_vault_orphans(vault)}\n"
        f"errors={result.errors}"
    )
    assert _vault_orphans(vault) == [], (
        "fragments were soft-tombed into 10-Liminal/Orphaned by a pass "
        "whose discovery had failed, not by their sources vanishing.\n\n"
        f"orphans={_vault_orphans(vault)}\nerrors={result.errors}"
    )
    assert result.tombed == 0, (
        "the tomb sweep ran on an incomplete discovery. An absent key "
        "cannot be proven absent when the walk that missed it failed.\n\n"
        f"tombed={result.tombed} discovered={result.discovered} "
        f"errors={result.errors}"
    )
    assert (result.discovered, result.written) == (2, 2), (
        "the harvest did not survive one unreadable file. The two readable "
        "entries must still be discovered and re-ingested — a per-file read "
        "failure costs that one file, never the pass. Letting the OSError "
        "propagate out of _read_directory instead would discard both, which "
        "is the silent-stoppage failure this design exists to avoid.\n\n"
        f"discovered={result.discovered} written={result.written} "
        f"errors={result.errors}"
    )


@requires_mode_bits
def test_an_unenumerable_subdirectory_does_not_orphan_the_corpus(
    tmp_path: Path,
) -> None:
    """The headline case: an unenumerable subtree buries the corpus SILENTLY.

    ``rglob`` swallows the ``scandir`` failure on ``src/sub``, so the walk
    returns zero paths and raises nothing. Discovery therefore reports
    *success with no documents*, which is indistinguishable — to
    ``tomb_missing_units`` — from an operator having deleted the entire
    source. Measured at HEAD: ``errors == []``, ``warnings == []``,
    ``tombed == 3``.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    sub = src / "sub"
    _write_corpus(sub)
    _control_pass(vault, src)

    with _unreadable(sub):
        result = _ingest(vault, src)

    assert len(_vault_fragments(vault)) == 3, (
        "an unenumerable subdirectory emptied the live fragment set. Every "
        "source file is still on disk; only the permission to list them "
        "changed.\n\n"
        f"live={_vault_fragments(vault)}\norphans={_vault_orphans(vault)}\n"
        f"errors={result.errors}"
    )
    assert _vault_orphans(vault) == [], (
        "the corpus was soft-tombed because a subtree could not be listed. "
        "An unlistable subtree is unknown, not empty.\n\n"
        f"orphans={_vault_orphans(vault)}\nerrors={result.errors}"
    )
    assert result.tombed == 0, (
        "the tomb sweep ran after a walk that could not enumerate the "
        "directory holding the entire corpus.\n\n"
        f"tombed={result.tombed} discovered={result.discovered} "
        f"errors={result.errors}"
    )


@requires_mode_bits
def test_an_unenumerable_source_root_does_not_orphan_the_corpus(
    tmp_path: Path,
) -> None:
    """An unenumerable source ROOT is the same silent burial, one level up.

    Nothing under the root can be reached, so discovery yields zero
    documents and — at HEAD — no complaint. The named source is the least
    plausible thing to have silently emptied itself, and it is exactly the
    shape a permissions change on a synced folder produces.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    _write_corpus(src)
    _control_pass(vault, src)

    with _unreadable(src):
        result = _ingest(vault, src)

    assert len(_vault_fragments(vault)) == 3, (
        "an unenumerable source root emptied the live fragment set. The "
        "root still holds all three entries; it just could not be "
        "listed.\n\n"
        f"live={_vault_fragments(vault)}\norphans={_vault_orphans(vault)}\n"
        f"errors={result.errors}"
    )
    assert _vault_orphans(vault) == [], (
        "the corpus was soft-tombed because the source root could not be "
        "listed.\n\n"
        f"orphans={_vault_orphans(vault)}\nerrors={result.errors}"
    )
    assert result.tombed == 0, (
        "the tomb sweep ran on a discovery that reached nothing at "
        "all.\n\n"
        f"tombed={result.tombed} discovered={result.discovered} "
        f"errors={result.errors}"
    )


# ---------------------------------------------------------------------------
# An incomplete pass must be audible — on both channels, at their own detail
# ---------------------------------------------------------------------------


@requires_mode_bits
def test_an_incomplete_pass_is_not_silent(tmp_path: Path) -> None:
    """A discovery that could not see everything must say so, twice.

    Disarming the sweep is necessary but not sufficient: a run that quietly
    ingests nothing looks identical to a run with nothing to do, and the
    operator would never learn that a folder went unreadable. So the
    incomplete pass reports on the error channel *and* raises an operator
    advisory on both ``warnings`` and ``ceiling_safe_warnings``.

    The two renderings differ on purpose. ``warnings`` is written for an
    operator at a terminal and names the source path and the number of
    fragments the sweep would have destroyed. ``ceiling_safe_warnings`` is
    the only advisory channel allowed to cross an MCP tier ceiling
    (#1372), so it must carry the finding without the operator's
    filesystem layout — hence the assertion that ``str(src)`` appears in no
    ceiling-safe entry.

    At HEAD this shape emits nothing on any of the three channels.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    sub = src / "sub"
    _write_corpus(sub)
    _control_pass(vault, src)

    with _unreadable(sub):
        result = _ingest(vault, src)

    assert result.errors, (
        "a pass that could not enumerate the subtree holding the entire "
        "corpus reported no error at all. rglob swallowed the scandir "
        f"failure.\n\nerrors={result.errors}"
    )
    assert result.warnings, (
        "no operator advisory was raised for an incomplete discovery, so "
        "the operator has no way to learn a folder went unreadable.\n\n"
        f"warnings={result.warnings}\nerrors={result.errors}"
    )
    assert result.ceiling_safe_warnings, (
        "the incomplete-discovery advisory has no ceiling-safe rendering, "
        "so an MCP caller — which sees only this channel — is told "
        "nothing.\n\n"
        f"ceiling_safe_warnings={result.ceiling_safe_warnings}\n"
        f"warnings={result.warnings}"
    )
    leaking = [entry for entry in result.ceiling_safe_warnings if str(src) in entry]
    assert leaking == [], (
        "the ceiling-safe advisory named the operator's source path. That "
        "channel is the one that crosses a tier ceiling, so it must carry "
        "the finding without the filesystem layout (#1372).\n\n"
        f"src={src}\nleaking={leaking}"
    )


# ---------------------------------------------------------------------------
# The deliberate broadening, and the harvest that survives it
# ---------------------------------------------------------------------------


@requires_mode_bits
def test_an_unrelated_unenumerable_subtree_still_disarms_the_sweep(
    tmp_path: Path,
) -> None:
    """A DELIBERATE BROADENING: any unenumerable subtree disarms the sweep.

    This test pins behaviour HEAD does not have and would not have needed.
    At HEAD an unreadable sibling directory holding no markdown is
    harmless — ``rglob`` skips it and the pass reports ``unchanged=3,
    tombed=0``. Under ``os.walk(..., onerror=...)`` the same directory
    fires ``onerror``, marks the discovery incomplete, and disarms the
    tomb sweep even though nothing the sweep cares about lived there.

    That is the judged trade, and the rationale is the same one that makes
    the primitive safe: if a subtree cannot be enumerated, a missing ledger
    key cannot be *proven* absent — the key's source might be sitting in
    the part of the tree the walk could not read. For a soft-delete
    primitive, "nothing is destroyed" is the only acceptable failure
    direction. The cost of the broadening is one skipped tombing pass; the
    cost of the narrow version is a destroyed corpus.

    The harvest surviving is what makes that trade payable, and it is the
    property that distinguishes this design from a bare ``raise OSError``:
    a re-raise would return ``[]`` from ``_discover_safe`` and turn one
    unreadable junk folder into a total ingest outage. Here the three
    readable entries are still ingested; only the destructive step stands
    down.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    _write_corpus(src)
    junk = src / "junk"
    junk.mkdir()
    (junk / "cache.bin").write_bytes(b"\x00\x01\x02")
    _control_pass(vault, src)

    with _unreadable(junk):
        result = _ingest(vault, src)

    assert result.written == 3, (
        "the harvest did not survive: an unreadable subtree that held no "
        "markdown stopped the readable entries from being ingested. A "
        "discovery failure must cost the tombing pass, never the "
        "ingest.\n\n"
        f"written={result.written} discovered={result.discovered} "
        f"errors={result.errors}"
    )
    assert result.tombed == 0, (
        "the tomb sweep ran despite a subtree the walk could not "
        "enumerate. A key missing from a partial walk is unknown, not "
        "absent.\n\n"
        f"tombed={result.tombed} errors={result.errors}"
    )
    assert result.warnings, (
        "no advisory was raised, so nothing observable distinguishes a "
        "pass with discovery_complete=False from a complete one — and the "
        "skipped tombing pass would go unexplained.\n\n"
        f"warnings={result.warnings}\nerrors={result.errors}"
    )
    assert _vault_orphans(vault) == [], (
        "fragments were soft-tombed by a pass whose walk hit an "
        "unenumerable subtree.\n\n"
        f"orphans={_vault_orphans(vault)}\nerrors={result.errors}"
    )


# ---------------------------------------------------------------------------
# The unattended `creek sync` surface — the silent-est instance of the defect
# ---------------------------------------------------------------------------


@requires_mode_bits
def test_the_unattended_sync_surface_is_told(tmp_path: Path) -> None:
    """``creek sync`` is the worst surface, and the callback is its only voice.

    ``creek/cli.py`` runs the journal source through ``_run_ingest`` with
    ``incremental=True`` and **without** ``print_summary=True``, so on the
    scheduled path the ``N tombed`` line is never printed at all. The
    operator's whole corpus can be buried by a launchd tick with nothing
    written to the terminal.

    Two things are pinned here that the tests above do not reach.

    First, incremental mode does not disarm the sweep on its own:
    ``run_ingest`` records ``seen_keys.add(origin_key)`` *before* the
    incremental skip precisely so an unchanged unit is never mistaken for a
    deleted one, which means an empty discovery still yields an empty seen
    set and still tombs everything. So the gate has to hold under
    ``incremental=True`` too.

    Second, the advisory must reach ``on_warning`` — the callback
    ``creek.cli._run_ingest`` wires to ``_print_ingest_warning`` for every
    caller, sync included. Appending to ``result.warnings`` without
    notifying is exactly the #1329 invisibility failure, and on this surface
    nobody ever reads the return value. Asserting the callback fired, rather
    than that the list is non-empty, is what distinguishes the two.

    Measured at HEAD on this shape: ``tombed == 3`` and the callback fired
    zero times.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    sub = src / "sub"
    _write_corpus(sub)
    _control_pass(vault, src)

    heard: list[str] = []
    with _unreadable(sub):
        result = run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["markdown"],
            source_type="markdown",
            input_path=src,
            vault_path=vault,
            incremental=True,
            on_warning=heard.append,
        )

    assert result.tombed == 0, (
        "incremental mode did not disarm the tomb sweep. seen_keys is "
        "populated before the incremental skip, so an empty discovery still "
        "orphans every live ledger key — on the one surface that prints no "
        "tomb count at all.\n\n"
        f"tombed={result.tombed} discovered={result.discovered} "
        f"errors={result.errors}"
    )
    assert len(_vault_fragments(vault)) == 3, (
        "an unattended sync tick emptied the live fragment set.\n\n"
        f"live={_vault_fragments(vault)}\norphans={_vault_orphans(vault)}"
    )
    assert heard, (
        "the incomplete-discovery advisory never reached on_warning, so the "
        "sync surface — which prints no summary and reads no return value — "
        "says nothing at all. An advisory recorded but not delivered is "
        "invisible (#1329).\n\n"
        f"heard={heard}\nwarnings={result.warnings}"
    )


# ---------------------------------------------------------------------------
# The walk rewrite must not change WHAT is discovered — only what is reported
# ---------------------------------------------------------------------------


def _mixed_tree(src: Path) -> None:
    """Build the fixture that separates the walk from the glob it replaced.

    Every entry is here because it lands differently in one of the two
    enumerations, or because it pins a match rule that must not drift:
    a hidden file, a nested file, a case-mismatched suffix, a symlink to a
    real file, a dangling symlink, a real *directory* named ``*.md``, and a
    symlink to a directory named ``*.md``.

    Args:
        src: Source root to build under.
    """
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    (src / ".hidden.md").write_text("# H\n\nhidden\n", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "c.md").write_text("# C\n\ncharlie\n", encoding="utf-8")
    (src / "B.MD").write_text("# B\n\nbravo\n", encoding="utf-8")
    (src / "linkfile.md").symlink_to(src / "a.md")
    (src / "dangling.md").symlink_to(src / "never-created.md")
    (src / "dir.md").mkdir()
    (src / "linkdir.md").symlink_to(src / "sub")


def test_the_walk_matches_the_md_suffix_case_sensitively(tmp_path: Path) -> None:
    """``B.MD`` is not a markdown source, and the rewrite must not make it one.

    Measured on a case-INSENSITIVE APFS volume: ``rglob("*.md")`` did not
    match ``B.MD``. So case-sensitivity is the behaviour every vault ingested
    before this change was built on, and ``name.lower().endswith(".md")``
    would not be a tidy-up — it would silently widen what ingest picks up,
    minting fragments for files no previous run ever saw.

    This assertion is kept even though CI's case-sensitive Linux filesystem
    cannot tell the two rules apart on a *different* pair of names: it is
    free, it holds on both platforms, and it fails loudly if someone later
    "simplifies" the suffix test.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    src = tmp_path / "src"
    _mixed_tree(src)

    discovered = {doc.path.name for doc in MarkdownIngestor().discover(src)}

    assert "B.MD" not in discovered, (
        "the walk matched an upper-case .MD suffix that rglob never "
        "matched, widening ingest beyond what every prior run saw.\n\n"
        f"discovered={sorted(discovered)}"
    )
    assert "a.md" in discovered, (
        "the lower-case control file was not discovered, so the assertion "
        f"above proves nothing about case at all.\n\n{sorted(discovered)}"
    )


def test_the_walk_discovers_exactly_what_the_glob_did(tmp_path: Path) -> None:
    """The rewrite changes how failures are reported, never what is found.

    The comparison is deliberately made **post-`is_file()`**. The raw sets
    genuinely differ and a naive parity assertion would go red on correct
    code: a real directory named ``dir.md`` and a symlinked directory named
    ``linkdir.md`` are yielded by ``rglob("*.md")`` but land in
    ``os.walk``'s ``dirnames``, never its ``filenames``. Measured, the raw
    glob gives seven entries and the raw walk five; after the filter both
    are the same four.

    Sorted order is asserted as a list, not a set, because the glob this
    replaces was ``sorted(dir_path.rglob("*.md"))`` and fragment ids are
    derived per file in the order discovery returns them.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    src = tmp_path / "src"
    _mixed_tree(src)

    walked = [doc.path for doc in MarkdownIngestor().discover(src)]
    globbed = [path for path in sorted(src.rglob("*.md")) if path.is_file()]

    assert walked == globbed, (
        "the os.walk rewrite discovers a different set — or a different "
        "order — than the sorted rglob it replaced.\n\n"
        f"walk={walked}\nglob={globbed}"
    )
    assert [path.name for path in walked] == [
        ".hidden.md",
        "a.md",
        "linkfile.md",
        "c.md",
    ], (
        "the discovered set is not the expected four post-filter entries, "
        "so the parity assertion above may be comparing two identically "
        f"broken walks.\n\nwalk={[p.name for p in walked]}"
    )
