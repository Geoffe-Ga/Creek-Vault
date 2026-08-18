"""Ingest advisories that may cross an MCP tier ceiling (#1372).

Three MCP tools compute operator advisories and drop them at the boundary,
so a caller is told nothing about a vault state the CLI console shouts
about. The obvious repair — echo ``IngestRunResult.warnings`` — is a
privacy defect: :func:`creek.ingest.pipeline.collapsed_unit_warning`
interpolates up to three REAL pre-existing vault fragment ids into its
text, and the MCP caller's ceiling may not admit those fragments.
``creek_mcp.tools.author`` already states the precedent for its own
findings: echoing them all "would undo the privacy scrub".

The answer this module pins is that an advisory which crosses the
boundary is content-free **by construction at the producer**. The
producer is the only place that knows what it interpolated; a
consumer-side allowlist cannot tell a safe advisory from an unsafe one
without re-parsing prose, and a downstream scrub would have to guess
which substrings are vault content. The leak in
:func:`test_the_boundary_never_echoes_the_unsafe_channel_at_the_narrowest_ceiling`
is deliberately a fragment id *and* a fragment title *and* a body
excerpt, because an id-shaped regex catches only the first of the three.

So :class:`~creek.ingest.pipeline.IngestRunResult` carries two channels:
``warnings`` (unchanged, full detail, for the terminal) and
``ceiling_safe_warnings`` (the subset proven free of vault content, the
only one an MCP tool may echo).

No test in this module may be skipped or xfailed: each one is either a
producer obligation or a privacy obligation, and a skipped privacy test
is a green gate over an open leak.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
from openpyxl import Workbook

from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.pipeline import (
    _COLLAPSED_UNIT_SAFE_TEMPLATE,
    _COLLAPSED_UNIT_WARNING_TEMPLATE,
    _UNPINNED_VAULT_WARNING,
    IngestRunResult,
    run_ingest,
)
from creek.ingest.spreadsheets import SpreadsheetIngestor
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.journal import journal_ingest_tool
from creek_mcp.tools.upload import upload_tool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TS = "2026-06-20T10:00:00+00:00"

_PINNED_MTIME = 1710493200.0
"""2024-03-15T09:00:00+00:00 — a fixed source mtime keeps ids stable."""

_PRE_FIX_ID = "frag-0ldc0ffee123"
"""Stands in for an id minted by the pre-#1329 derivation.

Deliberately not reproducible from the fragment's contents, so a run that
recomputed the id instead of finding it could not accidentally match.
"""

_BODY = "An untouched note.\n"

_SHEETS = ("Budget", "Notes", "Q3")
"""Sheet names for the workbook that forces the collapsed-unit advisory."""

_PINNING_ADVICE = "the vault needs pinning"
"""A stand-in safe advisory for the boundary tests.

Short and content-free on purpose: the boundary tests are about which
channel is echoed, not about the wording the producer chose.
"""

_LEAKED_ID = "frag-deadbeef1234"
_LEAKED_TITLE = "Therapy notes on my marriage"
_LEAKED_BODY = "I keep saying yes"

_UNSAFE_ADVISORY = (
    f"superseded: {_LEAKED_ID} ({_LEAKED_TITLE}) — {_LEAKED_BODY} to things "
    "I do not want"
)
"""What the operator channel is allowed to say, and the boundary is not.

Composed from the three needles rather than pasted, so the leak
assertions can never drift away from the string they are policing.
"""

_SAFE_ADVISORY = (
    "This vault holds 1 fragment(s) written before #1305. Run the same "
    "ingest at a terminal to see which fragments."
)
"""The content-free twin of :data:`_UNSAFE_ADVISORY`.

It still reports the data loss, and its count, because an advisory that
suppresses the number as well as the ids tells the caller nothing.
"""


# ---------------------------------------------------------------------------
# Producer fixtures — the real ``run_ingest``, no stubs
# ---------------------------------------------------------------------------


def _write_workbook(path: Path, names: tuple[str, ...]) -> Path:
    """Write a real XLSX at *path* with one populated sheet per name.

    Args:
        path: Destination file; parents are created.
        names: Sheet names in workbook order. Each sheet gets a header row
            and one data row so none is skipped as empty.

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


def _unpinned_run(root: Path) -> IngestRunResult:
    """Ingest a genuinely un-pinned vault and return the run's result.

    Reproduces the state the #1329 advisory exists for: one fragment on
    disk written under the old derivation, ``source.origin_key`` absent,
    and an empty markdown ledger.

    Args:
        root: A private directory to build the vault and source under.

    Returns:
        The pipeline result for the re-ingest that raises the advisory.
    """
    vault = root / "vault"
    for directory in ("00-Creek-Meta/Processing-Log", "01-Fragments/Unsorted"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    note = src / "note.md"
    note.write_text(_BODY, encoding="utf-8")
    os.utime(note, (_PINNED_MTIME, _PINNED_MTIME))
    fragment = Fragment(
        id=_PRE_FIX_ID,
        title=note.stem,
        created=datetime(2024, 3, 14, 19, 0, tzinfo=UTC),
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            original_file=str(note),
        ),
    )
    VaultWriter(vault_path=vault).write_fragment(fragment, body=_BODY)
    return run_ingest(
        ingestor_cls=MarkdownIngestor,
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )


def _collapsed_run(root: Path) -> tuple[IngestRunResult, str]:
    """Force the pre-#1305 collapsed-unit advisory and return it with its id.

    The recipe is the one that makes the advisory fire and no other:
    ingest a ONE-sheet workbook so a single whole-file fragment lands,
    then rewrite the same file with three sheets, restoring its mtime so
    identity is unchanged, and re-ingest. The old whole-file id is now
    superseded by three per-sheet ids and the advisory names it.

    Args:
        root: A private directory to build the vault and workbook under.

    Returns:
        The second run's result, and the superseded fragment's real id.
    """
    vault = root / "vault"
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    book = root / "src" / "book.xlsx"
    _write_workbook(book, ("Budget",))

    first = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
    )
    assert first.created == 1, first
    assert first.warnings == [], first
    collapsed_id = str(frontmatter.load(_fragment_files(vault)[0]).metadata["id"])

    mtime = book.stat().st_mtime
    _write_workbook(book, _SHEETS)
    os.utime(book, (mtime, mtime))

    second = run_ingest(
        ingestor_cls=SpreadsheetIngestor,
        source_type="spreadsheet",
        input_path=book,
        vault_path=vault,
    )
    assert second.created == 3, second
    return second, collapsed_id


# ---------------------------------------------------------------------------
# Group 1 — the producer populates both channels
# ---------------------------------------------------------------------------


def test_the_unpinned_vault_advisory_is_declared_ceiling_safe(
    tmp_path: Path,
) -> None:
    """The #1329 advisory is a fixed constant, so its safe form IS its own text.

    Nothing about this advisory is interpolated from the vault — it names
    a command, not a fragment — so withholding it from an MCP caller
    would be caution with no subject. The equality assertion is what
    stops a producer from playing safe by leaving the safe channel empty.
    """
    result = _unpinned_run(tmp_path)

    assert len(result.warnings) == 1, result.warnings
    assert result.ceiling_safe_warnings == result.warnings
    assert "--pin-source-ids" in result.ceiling_safe_warnings[0]


def test_the_collapsed_advisory_keeps_its_ids_off_the_safe_channel(
    tmp_path: Path,
) -> None:
    """The operator channel keeps the ids; the safe channel keeps the count.

    The load-bearing producer test. Every claim lives here together so
    the pair cannot pass by dropping one side: an implementation that
    silences the console loses (a), one that echoes the ids loses (c) and
    (e), and one that suppresses the advisory entirely loses (b) and (d).
    """
    result, collapsed_id = _collapsed_run(tmp_path)

    assert collapsed_id.startswith("frag-"), collapsed_id
    assert len(result.warnings) == 1, result.warnings
    assert collapsed_id in result.warnings[0]
    assert len(result.ceiling_safe_warnings) == 1, result.ceiling_safe_warnings
    safe = result.ceiling_safe_warnings[0]
    assert collapsed_id not in safe
    assert "1 fragment(s)" in safe
    assert "frag-" not in safe


def test_every_advisory_declares_a_disclosure(tmp_path: Path) -> None:
    """Both producers answer the disclosure question, and neither leaks an id.

    ``warn`` takes ``ceiling_safe`` as a keyword-only argument with no
    default precisely so a future producer cannot forget to decide. This
    is the runtime half of that: whatever each producer decided, the safe
    channel is a subset of the operator channel and holds no fragment id.
    """
    results = [
        _unpinned_run(tmp_path / "unpinned"),
        _collapsed_run(tmp_path / "collapsed")[0],
    ]

    for result in results:
        assert result.warnings, result
        assert len(result.ceiling_safe_warnings) <= len(result.warnings)
        assert not [text for text in result.ceiling_safe_warnings if "frag-" in text]


def test_the_templates_did_not_trade_places() -> None:
    """The console template keeps its sample; the safe one never gains one.

    An anti-vacuity guard for the two tests above. The cheapest way to
    make a leak test pass is to strip ``{sample}`` from the advisory
    altogether, which would silently cost the CLI operator the only
    detail that tells them which fragments to inspect.
    """
    assert "{" not in _UNPINNED_VAULT_WARNING
    assert "{sample}" in _COLLAPSED_UNIT_WARNING_TEMPLATE
    assert "{sample}" not in _COLLAPSED_UNIT_SAFE_TEMPLATE
    assert "{count}" in _COLLAPSED_UNIT_SAFE_TEMPLATE


# ---------------------------------------------------------------------------
# Group 2 — the boundary echoes only the safe channel
# ---------------------------------------------------------------------------


def _mcp_vault(tmp_path: Path) -> Path:
    """Create the minimum vault layout the writer, ledger and audit log need.

    Args:
        tmp_path: Pytest temp directory. The vault is a *subdirectory* so a
            fixture file written beside it never lands inside the vault.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


def _xlsx_base64(tmp_path: Path) -> str:
    """Return a genuine one-sheet ``.xlsx`` payload, base64-encoded.

    Derived at runtime rather than pasted as an opaque literal, matching
    the ``creek.upload`` suite's rule for binary fixtures.

    Args:
        tmp_path: Pytest temp directory to build the workbook under.

    Returns:
        The base64 text an MCP caller would send.
    """
    book = _write_workbook(tmp_path / "payload" / "book.xlsx", ("Budget",))
    return base64.b64encode(book.read_bytes()).decode("ascii")


def _runner_returning(
    *,
    ceiling_safe_warnings: list[str],
    warnings: list[str] | None = None,
) -> Callable[..., IngestRunResult]:
    """Build an ingest-runner stub that reports the advisories under test.

    ``written=1`` and ``errors=[]`` because ``creek.upload`` refuses a
    ``written == 0`` outcome as a silent drop; this stub has to look like
    a successful run so the response under test is the success dict.

    Args:
        ceiling_safe_warnings: The content-free channel the tool may echo.
        warnings: The full operator channel; defaults to the safe one when
            the two are the same.

    Returns:
        A callable accepted by the tools' ``run=`` seam.
    """
    operator = list(ceiling_safe_warnings) if warnings is None else list(warnings)

    def _run(**_kwargs: Any) -> IngestRunResult:
        """Return the fixed outcome, ignoring what the tool asked for."""
        return IngestRunResult(
            written=1,
            errors=[],
            discovered=1,
            created=1,
            updated=0,
            unchanged=0,
            tombed=0,
            skipped=0,
            warnings=operator,
            ceiling_safe_warnings=list(ceiling_safe_warnings),
        )

    return _run


def test_journal_carries_the_advisory_to_its_caller(tmp_path: Path) -> None:
    """``creek.journal`` puts the safe channel on its success payload.

    Before this change the tool built its response from the run result
    without ever reading an advisory off it, so an Adepthood caller
    ingesting into an un-pinned vault was told ``ok`` while every entry
    it sent was minting a duplicate.
    """
    vault = _mcp_vault(tmp_path)

    out = journal_ingest_tool(
        vault_path=vault,
        external_id="adep-1372-journal",
        tier="open",
        content="a plain entry",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        run=_runner_returning(ceiling_safe_warnings=[_PINNING_ADVICE]),
    )

    assert out["status"] == "ok", out
    assert out["warnings"] == [_PINNING_ADVICE]


def test_upload_carries_the_advisory_to_its_caller(tmp_path: Path) -> None:
    """``creek.upload`` is the surface that most needs the advisory.

    The collapsed-unit advisory reports ACTUAL DATA LOSS — superseded
    fragments left stranded in the vault — and it fires on the
    multi-sheet-workbook path, which is exactly ``creek.upload``'s path.
    A design that let only the #1329 pinning advisory across the boundary
    would leave this surface silent about the one thing it most needs to
    say.
    """
    vault = _mcp_vault(tmp_path)

    out = upload_tool(
        vault_path=vault,
        filename="book.xlsx",
        content_base64=_xlsx_base64(tmp_path),
        external_id="adep-1372-upload",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        run=_runner_returning(ceiling_safe_warnings=[_SAFE_ADVISORY]),
    )

    assert out["status"] == "ok", out
    assert out["warnings"] == [_SAFE_ADVISORY]


def test_the_boundary_never_echoes_the_unsafe_channel_at_the_narrowest_ceiling(
    tmp_path: Path,
) -> None:
    """At ``ceiling=open`` no vault content reaches the caller — and it still speaks.

    The injected operator advisory leaks three different shapes of vault
    content: a fragment id, a fragment TITLE, and a line of the body. An
    id-shaped regex on the way out would catch only the first, which is
    why the filter has to be a producer-side content-free template rather
    than a downstream scrub.

    The final assertion is the anti-vacuity half: suppressing the whole
    field would pass every "not in" check while telling the caller
    nothing, so the safe count must still be there.
    """
    unsafe = _runner_returning(
        ceiling_safe_warnings=[_SAFE_ADVISORY],
        warnings=[_UNSAFE_ADVISORY],
    )

    journal_out = journal_ingest_tool(
        vault_path=_mcp_vault(tmp_path / "journal"),
        external_id="adep-1372-leak-journal",
        tier="open",
        content="a plain entry",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.OPEN,
        run=unsafe,
    )
    upload_out = upload_tool(
        vault_path=_mcp_vault(tmp_path / "upload"),
        filename="book.xlsx",
        content_base64=_xlsx_base64(tmp_path),
        external_id="adep-1372-leak-upload",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.OPEN,
        run=unsafe,
    )

    for out in (journal_out, upload_out):
        assert out["status"] == "ok", out
        blob = json.dumps(out)
        assert _LEAKED_ID not in blob
        assert _LEAKED_TITLE not in blob
        assert _LEAKED_BODY not in blob
        assert "1 fragment(s)" in blob


def test_a_run_with_no_advisory_reports_an_empty_list(tmp_path: Path) -> None:
    """The ordinary run carries the key with an empty list, not no key at all.

    A field that appears only when something is wrong forces every caller
    to write ``out.get("warnings", [])`` and makes "this tool never warns"
    indistinguishable from "this build dropped the field".
    """
    vault = _mcp_vault(tmp_path)

    out = journal_ingest_tool(
        vault_path=vault,
        external_id="adep-1372-quiet",
        tier="open",
        content="a plain entry",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        run=_runner_returning(ceiling_safe_warnings=[]),
    )

    assert out["status"] == "ok", out
    assert out["warnings"] == []


def test_the_advisory_templates_are_all_present_and_non_empty() -> None:
    """Every constant this module reasons about exists and says something.

    The closing guard: this module's privacy claims are only as good as
    the constants they are asserted against, and an empty or missing
    template would make several "not in" assertions trivially true.
    """
    for template in (
        _UNPINNED_VAULT_WARNING,
        _COLLAPSED_UNIT_WARNING_TEMPLATE,
        _COLLAPSED_UNIT_SAFE_TEMPLATE,
    ):
        assert isinstance(template, str)
        assert template.strip()
