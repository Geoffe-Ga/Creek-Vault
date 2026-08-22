"""RTBF purge over a sub-unit ``source.origin_key`` (#1305).

Issue #1305 gives each sheet of a multi-sheet workbook its own fragment id
*and* its own ledger key, so an uploaded workbook's fragments now carry an
``origin_key`` of the form ``…/uploads/book.xlsx#Budget`` rather than the
bare staged path.

That is the point where a spreadsheet bugfix turns into a privacy question.
``PurgeEngine._purge_staged_source_entry`` deletes the staged source a
purged fragment points at, and it reaches it by resolving ``origin_key``
inside the vault. A key naming a sub-unit resolves to a path that is inside
the staging root but is **not a file**, so the ``is_file()`` guard returns
early and the raw workbook — the operator's actual document — survives the
erasure request meant to remove it.

These tests pin both halves of the answer: the sweep must follow a sub-unit
key back to the workbook, and it must not have loosened a single one of the
containment guards to do it. A key that is genuinely a ``#``-named file
still wins on the whole-key attempt, and a traversal key with a unit suffix
is still refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.ingest.journal_staging import UPLOAD_STAGING_RELDIR
from creek.ingest.source_unit import compose_source_unit, split_source_unit
from creek.purge import PurgeEngine

if TYPE_CHECKING:
    from pathlib import Path

_WORKBOOK_SECRET = "synthetic-workbook-secret-1305"
"""Sentinel written into the staged workbook so its survival is detectable."""


def _make_vault(tmp_path: Path) -> Path:
    """Create the minimal vault layout the purge engine requires.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for sub in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Documents",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# minimal marker for GAP-003\n", encoding="utf-8"
    )
    return vault


def _stage_workbook(vault: Path, name: str = "book.xlsx") -> Path:
    """Write a stand-in staged workbook carrying the secret sentinel.

    The bytes are not a real XLSX on purpose: this module tests the purge
    sweep's path handling, and a real container would only make the
    sentinel harder to assert on.

    Args:
        vault: Vault root.
        name: Staged filename, including its extension.

    Returns:
        The staged file's path.
    """
    staged = vault / UPLOAD_STAGING_RELDIR / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(f"workbook bytes {_WORKBOOK_SECRET}\n", encoding="utf-8")
    return staged


def _write_sheet_fragment(vault: Path, frag_id: str, origin_key: str) -> Path:
    """Write one sheet fragment whose ``source.origin_key`` is *origin_key*.

    Args:
        vault: Vault root.
        frag_id: The fragment's id.
        origin_key: The vault-relative source key, with or without a unit.

    Returns:
        The written fragment's path.
    """
    path = vault / "01-Fragments" / "Documents" / f"{frag_id}.md"
    post = frontmatter.Post(
        content="A rendered sheet table.\n",
        id=frag_id,
        title=frag_id,
        type="fragment",
        source={
            "platform": "spreadsheet",
            "origin_key": origin_key,
            "original_file": str(vault / UPLOAD_STAGING_RELDIR / "book.xlsx"),
        },
        threads=[],
        eddies=[],
        privacy_tier="intimate",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _survivors(vault: Path) -> list[Path]:
    """Return every file under *vault* whose text still holds the sentinel.

    Args:
        vault: Vault root to walk.

    Returns:
        Offending paths — empty when the purge honoured the request.
    """
    return [
        path
        for path in sorted(vault.rglob("*"))
        if path.is_file()
        and _WORKBOOK_SECRET in path.read_text(encoding="utf-8", errors="ignore")
    ]


def test_purge_follows_a_sub_unit_origin_key_to_the_staged_workbook(
    tmp_path: Path,
) -> None:
    """A ``…/book.xlsx#Budget`` key must still erase ``book.xlsx``.

    RED before the sub-unit fallback lands: the key resolves inside the
    staging root but names no file, so ``is_file()`` returns early and the
    workbook survives an RTBF request with ``journal_staged_removed == 0``.
    """
    vault = _make_vault(tmp_path)
    staged = _stage_workbook(vault)
    key = compose_source_unit(f"{UPLOAD_STAGING_RELDIR.as_posix()}/book.xlsx", "Budget")
    frag = _write_sheet_fragment(vault, "frag-sheet-budget", key)

    result = PurgeEngine(vault).purge_fragment("frag-sheet-budget")

    assert result.journal_staged_removed == 1
    assert not frag.exists()
    assert not staged.exists()
    assert _survivors(vault) == []


def test_purge_of_one_sheet_erases_the_workbook_shared_with_its_siblings(
    tmp_path: Path,
) -> None:
    """Erasing one sheet erases the shared source; siblings stay live.

    This is the semantics every multi-fragment source already has — purging
    one function fragment deletes the ``.py`` it came from — but it is newly
    reachable for spreadsheets, so it is pinned rather than left implicit.
    The sibling fragments are deliberately NOT deleted: this fixes an
    identity defect, and deciding that one sheet's erasure erases three
    fragments would be a policy change riding a bugfix.
    """
    vault = _make_vault(tmp_path)
    staged = _stage_workbook(vault)
    base = f"{UPLOAD_STAGING_RELDIR.as_posix()}/book.xlsx"
    frag_a = _write_sheet_fragment(vault, "frag-a", compose_source_unit(base, "Budget"))
    frag_b = _write_sheet_fragment(vault, "frag-b", compose_source_unit(base, "Notes"))

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert result.journal_staged_removed == 1
    assert not frag_a.exists()
    assert frag_b.exists()
    assert not staged.exists()


def test_purge_prefers_a_real_hash_named_file_over_the_unit_split(
    tmp_path: Path,
) -> None:
    """``report#1.xlsx`` is a filename, not a unit, and must be erased as one.

    ``#`` is legal in a POSIX filename, so the split is a hypothesis. The
    whole key is tried first; only a key that resolves to nothing falls back.
    Without that ordering this test's file survives while a *different*
    file named ``report`` would be targeted.
    """
    vault = _make_vault(tmp_path)
    staged = _stage_workbook(vault, name="report#1.xlsx")
    decoy = _stage_workbook(vault, name="report")
    key = f"{UPLOAD_STAGING_RELDIR.as_posix()}/report#1.xlsx"
    _write_sheet_fragment(vault, "frag-hashname", key)

    result = PurgeEngine(vault).purge_fragment("frag-hashname")

    assert result.journal_staged_removed == 1
    assert not staged.exists()
    assert decoy.exists(), "the split must not steer the delete at another file"


@pytest.mark.parametrize(
    "origin_key",
    [
        "../outside-secret.xlsx#Budget",
        "01-Fragments/other.md#Budget",
        f"{UPLOAD_STAGING_RELDIR.as_posix()}/../../../escape.xlsx#Budget",
    ],
)
def test_purge_still_refuses_an_out_of_scope_key_with_a_unit_suffix(
    tmp_path: Path, origin_key: str
) -> None:
    """Both containment guards must be re-applied on the fallback path.

    A sub-unit suffix must not become a way to smuggle a traversal or a
    non-staging vault path past guards the whole-key attempt enforces. The
    fallback only ever widens what is deleted *inside* the staging roots.
    """
    vault = _make_vault(tmp_path)
    target = vault / "outside-secret.xlsx"
    for candidate in (
        (vault.parent / "outside-secret.xlsx"),
        (vault / "01-Fragments" / "other.md"),
        (vault.parent.parent / "escape.xlsx"),
        target,
    ):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(f"decoy {_WORKBOOK_SECRET}\n", encoding="utf-8")
    frag = _write_sheet_fragment(vault, "frag-escape", origin_key)

    result = PurgeEngine(vault).purge_fragment("frag-escape")

    assert result.journal_staged_removed == 0
    assert not frag.exists()
    assert (vault.parent / "outside-secret.xlsx").exists()
    assert (vault / "01-Fragments" / "other.md").exists()
    assert (vault.parent.parent / "escape.xlsx").exists()
    # In-vault but outside every staging root — the second containment
    # guard's own case, and the one a fallback is most likely to relax.
    assert target.exists()
    assert _WORKBOOK_SECRET in target.read_text(encoding="utf-8")


def test_split_source_unit_round_trips_and_refuses_a_bare_separator() -> None:
    """The compose/split pair agrees with itself, including on the empty unit.

    ``compose_source_unit`` never mints a key ending in a bare separator, so
    ``split_source_unit`` must not invent a unit for one — two spellings of
    "the whole file" is how one fragment comes to be written twice.
    """
    assert split_source_unit(compose_source_unit("a/b.xlsx", "Budget")) == (
        "a/b.xlsx",
        "Budget",
    )
    assert compose_source_unit("a/b.xlsx", None) == "a/b.xlsx"
    assert compose_source_unit("a/b.xlsx", "") == "a/b.xlsx"
    assert split_source_unit("a/b.xlsx") == ("a/b.xlsx", None)
    assert split_source_unit("a/b.xlsx#") == ("a/b.xlsx#", None)
    # The LAST separator wins, so a sheet named with a '#' still splits back
    # to the workbook rather than to a prefix of its own name.
    assert split_source_unit("a/b.xlsx#Q#3") == ("a/b.xlsx#Q", "3")


def test_a_sheet_name_containing_the_separator_is_still_purgeable(
    tmp_path: Path,
) -> None:
    """A sheet named ``Rev#2`` must not mint a key nobody can read back.

    ``#`` is legal in an Excel sheet title (openpyxl round-trips one), so an
    unsanitised unit composes ``…/book.xlsx#Rev#2``. ``split_source_unit``
    splits at the LAST separator, yielding base ``…/book.xlsx#Rev`` — a file
    that does not exist — so the purge sweep finds no staged source, reports
    ``journal_staged_removed == 0``, and silently leaves the operator's
    workbook on disk after an erasure request.

    The fix is at mint time (``sanitize_unit``), not in the reader: the unit
    half of a composed key is now guaranteed separator-free, so exactly one
    rpartition is exact for every key Creek produces.
    """
    vault = _make_vault(tmp_path)
    staged = _stage_workbook(vault)
    key = compose_source_unit(f"{UPLOAD_STAGING_RELDIR.as_posix()}/book.xlsx", "Rev#2")

    assert key.count("#") == 1, key
    assert split_source_unit(key) == (
        f"{UPLOAD_STAGING_RELDIR.as_posix()}/book.xlsx",
        "Rev-2",
    )

    _write_sheet_fragment(vault, "frag-hash-sheet", key)
    result = PurgeEngine(vault).purge_fragment("frag-hash-sheet")

    assert result.journal_staged_removed == 1
    assert not staged.exists()
    assert _survivors(vault) == []


def test_separator_bearing_sheet_names_do_not_collide_after_sanitising() -> None:
    """``Rev#2`` and ``Rev-2`` sanitise alike and must still get distinct units.

    Sanitising *after* de-duplication would map two real sheets onto one
    unit, one id, and — with first-writer-wins — one surviving fragment:
    exactly the defect #1305 exists to fix, reintroduced by the fix for it.
    The de-duplicator therefore counts sanitised names.
    """
    from creek.ingest.spreadsheets import SheetData, _sheet_unit_keys

    sheets = [
        SheetData(name="Rev#2", headers=("a",), rows=(("1",),)),
        SheetData(name="Rev-2", headers=("a",), rows=(("2",),)),
    ]

    units = _sheet_unit_keys(sheets)

    assert units == ["Rev-2", "Rev-2~2"]
    assert len(set(units)) == 2
