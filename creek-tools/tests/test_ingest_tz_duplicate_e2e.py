"""Re-ingesting an untouched file from a differently-configured host (#1329).

The unit-level proof that a fragment id is host-dependent lives in
``tests/test_ingest_id_host_independence.py``. This module proves the
*consequence* end-to-end through the real shared pipeline: an operator who
moves a laptop across a timezone — or whose CI runs in UTC while they work in
Los Angeles — re-ingests the same unmodified files and silently doubles their
vault.

Three properties are pinned here, and each one is a separate failure mode:

* **Markdown** is ledger-wired, and the ledger still did not save it: the
  *unchanged* branch of ``write_fragment_idempotent`` wrote the fragment under
  its freshly-*derived* id rather than the ledger's recorded one.
* **Documents** are unledgered by default (``ledger_for_source`` returns
  ``None`` for every source type but ``markdown``), so nothing but a stable
  derivation stands between them and an unconditional second write.
* The ledger must be the **authority on identity** even if some future
  derivation change drifts again — the forcing-function test at the bottom
  pins that independently of any timezone.

Every case drives the **default, non-incremental** path. ``should_skip_unit``
returns early for an unchanged ledgered unit under ``--incremental``/``since``,
so an incremental-mode test would be green while the bug was live.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.ingest import pipeline as ingest_pipeline
from creek.ingest.documents import DocumentIngestor
from creek.ingest.ledger import SourceLedger
from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.pipeline import run_ingest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from creek.ingest.base import IngestedFragment, ParsedFragment
    from creek.ingest.pipeline import IngestRunResult

PINNED_MTIME = 1710493200.0
"""2024-03-15T09:00:00+00:00 — the mtime every source file here is pinned to."""

FIRST_ZONE = "UTC"
"""The zone the vault is first populated under."""

SECOND_ZONE = "Australia/Sydney"
"""The zone the *identical, untouched* tree is re-ingested under.

Sydney is +11 from UTC in March, so a naive derivation lands the fragment on a
different wall-clock hour (and, for other zones in the matrix, a different
calendar day) — guaranteeing a different hashed timestamp and hence a
different id.
"""

_BODY = "An untouched note.\n"


@pytest.fixture
def restore_host_tz() -> Iterator[None]:
    """Restore the process timezone after a test perturbs ``TZ``.

    ``time.tzset()`` mutates process-global state that ``monkeypatch.setenv``
    cannot undo on its own.

    Yields:
        ``None``; restoration happens on teardown.
    """
    original = os.environ.get("TZ")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree :class:`VaultWriter` needs.

    Args:
        tmp_path: Pytest temp directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for directory in ("00-Creek-Meta/Processing-Log", "01-Fragments/Unsorted"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    return vault


def _make_source_dir(tmp_path: Path, filename: str) -> Path:
    """Write a single pinned-mtime source file into a directory outside the vault.

    Args:
        tmp_path: Pytest temp directory.
        filename: Name of the single file to create.

    Returns:
        The containing directory, suitable as ``input_path``.
    """
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    note = src / filename
    note.write_text(_BODY, encoding="utf-8")
    os.utime(note, (PINNED_MTIME, PINNED_MTIME))
    return src


def _fragment_files(vault: Path) -> list[Path]:
    """Return every fragment markdown file written under ``01-Fragments``.

    Args:
        vault: Vault root.

    Returns:
        Sorted list of fragment file paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _ingest_under(
    tz_name: str,
    *,
    ingestor_cls: type[MarkdownIngestor] | type[DocumentIngestor],
    source_type: str,
    src: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> IngestRunResult:
    """Run one full ingest with the host timezone set to *tz_name*.

    Deliberately passes neither ``incremental`` nor ``since``: the default
    path is the only one that exercises the write.

    Args:
        tz_name: IANA zone to set as the host ``TZ``.
        ingestor_cls: Concrete ingestor to drive.
        source_type: Registry key for the ingestor.
        src: Input directory.
        vault: Vault root.
        monkeypatch: Pytest fixture used to set ``TZ``.

    Returns:
        The pipeline's structured result.
    """
    monkeypatch.setenv("TZ", tz_name)
    time.tzset()
    return run_ingest(
        ingestor_cls=ingestor_cls,
        source_type=source_type,
        input_path=src,
        vault_path=vault,
    )


@pytest.mark.usefixtures("restore_host_tz")
def test_markdown_reingest_under_a_different_timezone_does_not_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untouched markdown tree re-ingested from another zone stays one fragment.

    Pre-fix the second pass took ``write_fragment_idempotent``'s *unchanged*
    branch — correctly identifying the unit as known and unmodified — and then
    wrote it anyway under a newly-derived id, leaving two files and one
    orphan.
    """
    vault = _make_vault(tmp_path)
    src = _make_source_dir(tmp_path, "note.md")

    first = _ingest_under(
        FIRST_ZONE,
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        src=src,
        vault=vault,
        monkeypatch=monkeypatch,
    )
    assert first.errors == []
    assert first.created == 1
    assert len(_fragment_files(vault)) == 1
    original_id = str(frontmatter.load(str(_fragment_files(vault)[0]))["id"])

    second = _ingest_under(
        SECOND_ZONE,
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        src=src,
        vault=vault,
        monkeypatch=monkeypatch,
    )

    assert second.errors == []
    assert second.unchanged == 1
    assert second.created == 0
    files = _fragment_files(vault)
    assert len(files) == 1
    assert str(frontmatter.load(str(files[0]))["id"]) == original_id

    ledger = SourceLedger.load(vault, source="markdown")
    assert len(ledger.live_keys()) == 1


@pytest.mark.usefixtures("restore_host_tz")
def test_document_reingest_under_a_different_timezone_does_not_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untouched document re-ingested from another zone stays one fragment.

    Documents are the harder case: ``ledger_for_source`` returns ``None`` for
    them, so there is no record to fall back on and no ``origin_key`` to match.
    The *only* thing holding the vault at one file is the derived id staying
    stable, which lets the writer's per-directory id index recognise the
    second write as the same model. This case therefore asserts the file count
    but deliberately does **not** assert a ledger record.
    """
    vault = _make_vault(tmp_path)
    src = _make_source_dir(tmp_path, "note.txt")

    first = _ingest_under(
        FIRST_ZONE,
        ingestor_cls=DocumentIngestor,
        source_type="documents",
        src=src,
        vault=vault,
        monkeypatch=monkeypatch,
    )
    assert first.errors == []
    assert len(_fragment_files(vault)) == 1
    original_id = str(frontmatter.load(str(_fragment_files(vault)[0]))["id"])

    second = _ingest_under(
        SECOND_ZONE,
        ingestor_cls=DocumentIngestor,
        source_type="documents",
        src=src,
        vault=vault,
        monkeypatch=monkeypatch,
    )

    assert second.errors == []
    files = _fragment_files(vault)
    assert len(files) == 1
    assert str(frontmatter.load(str(files[0]))["id"]) == original_id


def test_borrowing_a_ledger_for_identity_does_not_arm_the_tomb_sweep(
    tmp_path: Path,
) -> None:
    """``ledger_source`` opts a source into pinned identity, not into tombing.

    ``resolve_ledger`` lets a non-markdown caller borrow a ledger purely to get
    ledger-backed identity — that is how an uploaded document earns its
    ``origin_key`` and therefore its RTBF purge coverage. Until the
    ``TOMBING_SOURCES`` split, that same opt-in also armed
    ``tomb_missing_units`` for any *directory* input, so one directory ingest
    under a borrowed ledger would soft-tomb every previously-recorded unit it
    did not happen to see. Identity and tombing are separate questions and are
    now gated separately.

    No behaviour changes for markdown, which remains the one tombing source.
    """
    vault = _make_vault(tmp_path)
    src = _make_source_dir(tmp_path, "note.txt")

    first = run_ingest(
        ingestor_cls=DocumentIngestor,
        source_type="documents",
        input_path=src,
        vault_path=vault,
        ledger_source="uploads",
    )
    assert first.errors == []
    assert first.created == 1

    # A second directory pass that no longer sees the first unit at all.
    other = tmp_path / "other"
    other.mkdir()
    later = other / "later.txt"
    later.write_text("A different note.\n", encoding="utf-8")
    os.utime(later, (PINNED_MTIME, PINNED_MTIME))

    second = run_ingest(
        ingestor_cls=DocumentIngestor,
        source_type="documents",
        input_path=other,
        vault_path=vault,
        ledger_source="uploads",
    )

    assert second.errors == []
    assert second.tombed == 0
    assert len(_fragment_files(vault)) == 2


def test_unchanged_branch_reuses_the_ledgered_id_not_the_derived_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger, not the derivation, is the authority on a known unit's id.

    This is the forcing function, and it is independent of #1329's timezone
    cause. Even after the derivation is made host-independent, *any* future
    change to how an id is derived would silently duplicate every ledgered
    fragment as long as the unchanged branch writes the derived id. Here the
    derivation is perturbed directly — as a future refactor might perturb it
    accidentally — and the pipeline must still write under the recorded id.
    """
    vault = _make_vault(tmp_path)
    src = _make_source_dir(tmp_path, "note.md")

    first = run_ingest(
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )
    assert first.created == 1
    ledgered_id = str(frontmatter.load(str(_fragment_files(vault)[0]))["id"])

    real_assemble = ingest_pipeline.assemble_ingested_fragment

    def _drifted(parsed: ParsedFragment) -> IngestedFragment:
        """Assemble normally, then perturb the derived id as a drift would."""
        assembled = real_assemble(parsed)
        assembled.fragment.id = "frag-ffffffffffff"
        return assembled

    monkeypatch.setattr(ingest_pipeline, "assemble_ingested_fragment", _drifted)

    second = run_ingest(
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )

    assert second.errors == []
    assert second.unchanged == 1
    files = _fragment_files(vault)
    assert len(files) == 1
    assert str(frontmatter.load(str(files[0]))["id"]) == ledgered_id
