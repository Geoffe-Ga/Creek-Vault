"""Multi-sheet workbooks on the surfaces that bypass ``run_ingest`` (#1305).

Three of the four ways a workbook reaches a vault go through
:func:`creek.ingest.pipeline.run_ingest`; two do not, and a
``run_ingest``-only test therefore certifies nothing about them:

* the ``creek.ingest`` MCP tool calls ``writer.write_fragment`` itself and
  builds its audit record from the ids it collected on the way, so before
  #1305 it recorded the SAME id N times in ``affected_fragment_ids`` — a
  false audit record, and audit records are what an RTBF request is
  answered from;
* ``creek process`` assembles every fragment and writes them in a later
  stage, having classified all N first, so before #1305 it paid N-1 LLM
  classification calls and then discarded their results when the writer
  deduped the ids.

The last test covers the un-migrated-vault advisory, which is this
change's answer to the fragments the old derivation left behind.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import frontmatter
import pytest
from openpyxl import Workbook

from creek.config import CreekConfig
from creek.ingest.base import generate_fragment_id
from creek.ingest.pipeline import run_ingest
from creek.ingest.spreadsheets import SpreadsheetIngestor
from creek.pipeline import Pipeline
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.ingest import ingest_tool

if TYPE_CHECKING:
    from pathlib import Path

_SHEET_NAMES = ("Budget", "Notes", "Q3")


def _write_workbook(path: Path, names: tuple[str, ...] = _SHEET_NAMES) -> Path:
    """Write a real multi-sheet XLSX at *path* and return it.

    Args:
        path: Destination file.
        names: Sheet names, in workbook order. Each sheet gets a header
            row and one data row so none is skipped as empty.

    Returns:
        *path*, for chaining.
    """
    workbook = Workbook()
    first = workbook.active
    assert first is not None
    first.title = names[0]
    for index, name in enumerate(names):
        sheet = first if index == 0 else workbook.create_sheet(name)
        sheet["A1"] = "label"
        sheet["B1"] = "value"
        sheet["A2"] = name
        sheet["B2"] = index
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def _fragment_files(vault: Path) -> list[Path]:
    """Return every persisted fragment file, sorted by path.

    Args:
        vault: Vault root.

    Returns:
        The fragment paths under ``01-Fragments``.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _scaffold(tmp_path: Path) -> Path:
    """Create the minimal vault layout ``VaultWriter`` and the audit log need.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


def test_mcp_ingest_tool_returns_and_audits_one_id_per_sheet(
    tmp_path: Path,
) -> None:
    """The ``creek.ingest`` tool must report three distinct ids, not one thrice.

    Pre-fix this returned ``written=3`` with three IDENTICAL entries in
    ``affected_fragment_ids`` while only one file reached disk: a run that
    over-reports what it wrote and names a fragment twice that does not
    exist. This surface never calls ``run_ingest``, so the ledgered and
    unledgered regression tests do not reach it.
    """
    vault = _scaffold(tmp_path)
    source = vault / "00-Inbox"
    _write_workbook(source / "book.xlsx")

    result = ingest_tool(
        vault_path=vault,
        source_type="spreadsheet",
        input_path=str(source),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert result["status"] == "ok"
    affected = result["affected_fragment_ids"]
    assert len(affected) == 3
    assert len(set(affected)) == 3
    assert result["written"] == 3
    files = _fragment_files(vault)
    assert len(files) == 3, [p.name for p in files]
    assert {frontmatter.load(p).metadata["id"] for p in files} == set(affected)

    entries = [
        json.loads(line)
        for line in (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    logged = [
        e["affected_fragment_ids"] for e in entries if e.get("affected_fragment_ids")
    ]
    assert len(logged) == 1
    assert sorted(logged[0]) == sorted(affected)
    assert len(set(logged[0])) == 3, "the audit record must not repeat one id"


@pytest.mark.e2e
def test_creek_process_writes_one_file_per_sheet(tmp_path: Path) -> None:
    """``creek process`` persists all three sheets, not just the first.

    ``Pipeline`` assembles every fragment, classifies them, and writes in a
    later stage. Pre-fix the three sheets shared one id, so the writer's
    dedup kept the first and dropped two — after they had each been
    classified. The wasted classification calls are a side effect this fix
    removes; the assertion here is about what reaches disk.
    """
    from creek.scaffold import scaffold_vault

    vault = tmp_path / "vault"
    scaffold_vault(vault)
    source = tmp_path / "source"
    _write_workbook(source / "book.xlsx")

    # ``no_llm=True`` keeps this hermetic: without it the run reaches the
    # LLM classifier on any machine with ollama up or a provider key set,
    # which is network egress from a test that is about file counts.
    result = Pipeline(config=CreekConfig(), no_llm=True).run(
        source_path=source, vault_path=vault
    )

    assert result.errors == []
    files = _fragment_files(vault)
    assert len(files) == 3, [p.name for p in files]
    assert result.fragments_created == 3
    headings = sorted(
        line
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("# ")
    )
    assert headings == [f"# book.xlsx — {name}" for name in sorted(_SHEET_NAMES)]
    # The writer de-collides a name clash with a ``-N`` suffix rather than
    # overwriting, so a ``-1`` stem is the on-disk signature of the defect.
    assert not [p.name for p in files if p.stem.endswith("-1")]


def test_unmigrated_vault_is_warned_about_not_silently_duplicated(
    tmp_path: Path,
) -> None:
    """A pre-#1305 collapsed fragment is named, and nothing is deleted.

    Seeds the exact state an operator's vault is in: one workbook already
    ingested under the old derivation, leaving a single fragment holding
    the first sheet. The next ingest writes the three per-sheet fragments
    and must say so — the superseded fragment is left in place, because
    deleting vault content is not a decision an ingest run gets to make
    (the same answer #1304 gave the same shape of problem).
    """
    vault = _scaffold(tmp_path)
    book = _write_workbook(tmp_path / "src" / "book.xlsx")

    # The pre-#1305 state, reproduced through the real derivation: with a
    # single non-empty sheet no unit is emitted, so this run writes exactly
    # the fragment the old code wrote for the whole workbook.
    legacy_book = tmp_path / "src2" / "book.xlsx"
    _write_workbook(legacy_book, names=("Budget",))
    legacy = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=legacy_book,
        vault_path=vault,
    )
    assert legacy.created == 1
    assert legacy.warnings == []
    seeded = _fragment_files(vault)
    assert len(seeded) == 1

    warnings: list[str] = []
    result = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
        on_warning=warnings.append,
    )

    assert result.created == 3
    assert result.warnings == warnings
    assert len(warnings) == 0, "a different workbook must not raise the advisory"
    # Four files: the seeded single-sheet fragment plus three per-sheet ones.
    assert len(_fragment_files(vault)) == 4
    assert seeded[0].exists(), "nothing is deleted by an ingest run"


def test_collapsed_fragment_from_the_same_workbook_raises_the_advisory(
    tmp_path: Path,
) -> None:
    """Re-ingesting the very workbook that collapsed names its stray fragment.

    The superseded id is recomputable exactly — it is what this same
    fragment hashes to with no unit — so detection needs no vault walk, no
    frontmatter parsing and no ``platform`` heuristic. The advisory is
    self-clearing: purge the named fragment and the next run is quiet.
    """
    vault = _scaffold(tmp_path)
    book = tmp_path / "src" / "book.xlsx"
    _write_workbook(book, names=("Budget",))

    first = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
    )
    assert first.created == 1
    assert first.warnings == []
    collapsed = _fragment_files(vault)[0]
    collapsed_id = str(frontmatter.load(collapsed).metadata["id"])

    # The operator adds two sheets to the same file. The workbook now yields
    # three units, so the old whole-file id is superseded.
    mtime = book.stat().st_mtime
    _write_workbook(book)
    os.utime(book, (mtime, mtime))

    warnings: list[str] = []
    second = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
        on_warning=warnings.append,
    )

    assert second.created == 3
    assert len(warnings) == 1
    assert collapsed_id in warnings[0]
    assert "Nothing is deleted automatically" in warnings[0]
    assert "--dry-run" in warnings[0]
    assert second.warnings == warnings
    assert collapsed.exists(), "the advisory reports; it never deletes"
    assert len(_fragment_files(vault)) == 4

    # Self-clearing: with the superseded fragment gone the run goes quiet.
    collapsed.unlink()
    third_warnings: list[str] = []
    run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
        on_warning=third_warnings.append,
    )
    assert third_warnings == []


def test_each_sheet_gets_its_own_provenance_entry(tmp_path: Path) -> None:
    """Provenance must record three distinct ids, not one three times.

    ``Ingestor._process_fragment`` mints the provenance id from a SECOND,
    independent call to ``generate_fragment_id``. Fixing only the call in
    ``assemble_ingested_fragment`` leaves the append-only provenance log —
    the record of what this ingest actually did — claiming one fragment was
    written three times. Caught by mutating that call site back to
    ``source_path``, which every other test in this change survived.
    """
    book = _write_workbook(tmp_path / "src" / "book.xlsx")

    result = SpreadsheetIngestor().ingest(book)

    assert len(result.fragments) == 3
    provenance_ids = [entry.fragment_id for entry in result.provenance]
    assert len(provenance_ids) == 3
    assert len(set(provenance_ids)) == 3
    assert set(provenance_ids) == {
        generate_fragment_id(parsed.identity_key, parsed.timestamp, parsed.content)
        for parsed in result.fragments
    }


def test_single_sheet_re_ingest_raises_no_collapsed_advisory(tmp_path: Path) -> None:
    """Re-ingesting an unchanged one-sheet workbook must stay quiet.

    The advisory recomputes "the id this fragment would have had with no
    unit". For a unit-less fragment that is simply its OWN id, which the
    vault holds from the previous run — so without the ``source_unit is
    None`` guard every re-ingest of every CSV and one-sheet XLSX would
    accuse the operator of an un-migrated vault. A warning that fires on
    correct state is a warning that gets trained away.
    """
    vault = _scaffold(tmp_path)
    book = tmp_path / "src" / "book.csv"
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text("a,b\n1,2\n", encoding="utf-8")

    first = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
    )
    warnings: list[str] = []
    second = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
        on_warning=warnings.append,
    )

    assert first.warnings == []
    assert second.warnings == []
    assert warnings == []
    assert len(_fragment_files(vault)) == 1


def test_collapsed_advisory_names_each_superseded_id_once(tmp_path: Path) -> None:
    """One superseded fragment must be reported as one, not once per sheet.

    All N sheets of a workbook recompute the SAME superseded id — that is
    the defect — so an undeduplicated advisory reports ``3 fragment(s)``
    and lists the identical id three times, overstating by a factor of N
    exactly how much stray content the operator has to review.
    """
    vault = _scaffold(tmp_path)
    book = tmp_path / "src" / "book.xlsx"
    _write_workbook(book, names=("Budget",))
    run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
    )
    mtime = book.stat().st_mtime
    _write_workbook(book)
    os.utime(book, (mtime, mtime))

    warnings: list[str] = []
    run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
        on_warning=warnings.append,
    )

    assert len(warnings) == 1
    assert "1 fragment(s)" in warnings[0]
    collapsed_id = str(
        frontmatter.load(
            next(p for p in _fragment_files(vault) if p.name.endswith("-book.md"))
        ).metadata["id"]
    )
    assert warnings[0].count(collapsed_id) == 1
