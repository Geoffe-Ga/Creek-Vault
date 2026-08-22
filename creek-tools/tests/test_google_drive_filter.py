"""Tests for creek.clean.filters.google_drive — Google Drive pre-ingestion filter.

Tests cover:
- StagedFile model (path, filename, content, modified, authors, owner)
- GoogleDriveFilterResult model (action, reasons, duplicate_of)
- Duplicate detection: "Copy of..." prefix removal and version suffixes
- Year vs counter disambiguation: 4-digit year suffixes like (2021) are
  distinct documents, not Drive copy counters (issue #834)
- Empty document filtering: no extractable text
- Multi-author flagging: collaborator contributions exceed threshold
- Staleness detection: files not modified within configurable timeframe
- Cross-format deduplication: identical content in .docx and .pdf
- Batch filtering: filter_batch processes multiple files
- Configuration: all thresholds configurable via constructor
- Edge cases: unicode filenames, empty batches, single-file batches
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from creek.clean.filters.google_drive import (
    GoogleDriveFilter,
    GoogleDriveFilterResult,
    StagedFile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 5, 12, 0, 0, tzinfo=UTC)
"""Fixed "now" timestamp for deterministic tests."""


def _make_file(
    filename: str = "document.docx",
    content: str = "This is a real document with meaningful content.",
    modified: datetime | None = None,
    authors: list[str] | None = None,
    owner: str = "alice@example.com",
    size_bytes: int = 1024,
) -> StagedFile:
    """Create a StagedFile with sensible defaults for testing."""
    return StagedFile(
        path=Path(f"/staged/{filename}"),
        filename=filename,
        content=content,
        modified=modified or _NOW,
        authors=authors or [owner],
        owner=owner,
        size_bytes=size_bytes,
    )


# ---------------------------------------------------------------------------
# StagedFile model
# ---------------------------------------------------------------------------


class TestStagedFile:
    """Tests for the StagedFile Pydantic model."""

    def test_valid_staged_file(self) -> None:
        """Valid StagedFile should be created with all fields."""
        sf = _make_file()
        assert sf.filename == "document.docx"
        assert sf.content == "This is a real document with meaningful content."
        assert sf.owner == "alice@example.com"
        assert sf.size_bytes == 1024

    def test_staged_file_default_authors(self) -> None:
        """Authors list defaults to empty when not provided."""
        sf = StagedFile(
            path=Path("/staged/doc.docx"),
            filename="doc.docx",
            content="text",
            modified=_NOW,
            owner="alice@example.com",
        )
        assert sf.authors == []

    def test_staged_file_default_size(self) -> None:
        """Size defaults to zero when not provided."""
        sf = StagedFile(
            path=Path("/staged/doc.docx"),
            filename="doc.docx",
            content="text",
            modified=_NOW,
            owner="alice@example.com",
        )
        assert sf.size_bytes == 0


# ---------------------------------------------------------------------------
# GoogleDriveFilterResult model
# ---------------------------------------------------------------------------


class TestGoogleDriveFilterResult:
    """Tests for the GoogleDriveFilterResult Pydantic model."""

    def test_keep_result(self) -> None:
        """Keep result should have correct fields."""
        result = GoogleDriveFilterResult(
            action="keep",
            reasons=[],
        )
        assert result.action == "keep"
        assert result.reasons == []
        assert result.duplicate_of is None

    def test_skip_result_with_reason(self) -> None:
        """Skip result should store reasons."""
        result = GoogleDriveFilterResult(
            action="skip",
            reasons=["Empty document"],
        )
        assert result.action == "skip"
        assert len(result.reasons) == 1

    def test_flag_result_with_duplicate(self) -> None:
        """Flag result can reference a duplicate."""
        result = GoogleDriveFilterResult(
            action="skip",
            reasons=["Duplicate of original"],
            duplicate_of="/staged/original.docx",
        )
        assert result.duplicate_of == "/staged/original.docx"

    def test_invalid_action_rejected(self) -> None:
        """Invalid action value should be rejected by Pydantic."""
        with pytest.raises(ValidationError):
            GoogleDriveFilterResult(
                action="invalid",  # type: ignore[arg-type]
                reasons=[],
            )


# ---------------------------------------------------------------------------
# Duplicate detection: "Copy of..." prefix
# ---------------------------------------------------------------------------


class TestCopyOfDetection:
    """Tests for detecting 'Copy of...' filename duplicates."""

    def test_copy_of_prefix_detected(self) -> None:
        """Files named 'Copy of X' should be skipped as duplicates."""
        original = _make_file(filename="Budget 2026.xlsx")
        copy = _make_file(filename="Copy of Budget 2026.xlsx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([original, copy])
        copy_result = results[1]

        assert copy_result.action == "skip"
        assert any("copy of" in r.lower() for r in copy_result.reasons)

    def test_copy_of_case_insensitive(self) -> None:
        """'copy of' detection should be case-insensitive."""
        original = _make_file(filename="notes.docx")
        copy = _make_file(filename="copy of notes.docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([original, copy])
        assert results[1].action == "skip"

    def test_copy_of_without_original_still_flagged(self) -> None:
        """'Copy of...' file without original in batch should still be skipped."""
        copy = _make_file(filename="Copy of Missing Doc.docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([copy])
        assert results[0].action == "skip"

    def test_copy_of_references_original(self) -> None:
        """Copy should reference the original file path as duplicate_of."""
        original = _make_file(filename="Report.docx")
        copy = _make_file(filename="Copy of Report.docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([original, copy])
        assert results[1].duplicate_of == str(original.path)


# ---------------------------------------------------------------------------
# Duplicate detection: version suffixes
# ---------------------------------------------------------------------------


class TestVersionSuffixDetection:
    """Tests for detecting version suffix duplicates like (1), (2)."""

    def test_version_suffix_detected(self) -> None:
        """Files with (1), (2) suffixes should be skipped, keeping newest."""
        base = _make_file(
            filename="report.docx",
            modified=_NOW - timedelta(days=5),
        )
        v1 = _make_file(
            filename="report (1).docx",
            modified=_NOW - timedelta(days=3),
        )
        v2 = _make_file(
            filename="report (2).docx",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([base, v1, v2])

        # Newest version (v2) should be kept; others skipped
        assert results[2].action == "keep" or results[2].action == "flag"
        assert results[0].action == "skip"
        assert results[1].action == "skip"

    def test_version_suffix_various_numbers(self) -> None:
        """Various version numbers should be detected."""
        original = _make_file(
            filename="notes.pdf",
            modified=_NOW,
        )
        v1 = _make_file(
            filename="notes (1).pdf",
            modified=_NOW - timedelta(hours=1),
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([original, v1])
        assert results[1].action == "skip"
        assert any("version" in r.lower() for r in results[1].reasons)

    def test_version_suffix_preserves_newest(self) -> None:
        """The newest file in a version group should be preserved."""
        old_original = _make_file(
            filename="draft.docx",
            modified=_NOW - timedelta(days=30),
        )
        newer_copy = _make_file(
            filename="draft (1).docx",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([old_original, newer_copy])

        # newer_copy is newest, so it should be kept
        kept = [i for i, r in enumerate(results) if r.action != "skip"]
        assert len(kept) >= 1
        # The newest should not be skipped as version duplicate
        assert not any(
            "version" in r.lower()
            for r in results[1].reasons
            if results[1].action == "skip"
        )

    def test_two_digit_counter_still_deduped(self) -> None:
        """Two-digit Drive copy counters like (10) should still dedupe."""
        base = _make_file(
            filename="report.docx",
            modified=_NOW - timedelta(days=2),
        )
        v10 = _make_file(
            filename="report (10).docx",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([base, v10])

        assert results[0].action == "skip"
        assert any("version" in r.lower() for r in results[0].reasons)
        assert results[0].duplicate_of == str(v10.path)
        assert results[1].action == "keep"

    def test_three_digit_number_not_treated_as_version(self) -> None:
        """Parenthesised 3-digit numbers are not Drive copy counters.

        Drive only generates 1-2 digit copy counters, so ``(100)`` and
        ``(2021)`` are part of the document title and must not be
        stripped into a shared version group (issue #834 boundary).
        """
        ledger_100 = _make_file(
            filename="Ledger (100).pdf",
            content="Ledger covering the first one hundred transactions.",
            modified=_NOW - timedelta(days=1),
        )
        ledger_2021 = _make_file(
            filename="Ledger (2021).pdf",
            content="Ledger covering the 2021 fiscal year in full.",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([ledger_100, ledger_2021])

        assert results[0].action == "keep"
        assert results[1].action == "keep"
        for result in results:
            assert not any("version" in r.lower() for r in result.reasons)

    def test_version_counter_no_extension_still_deduped(self) -> None:
        """Version counters on extension-less filenames should still dedupe."""
        base = _make_file(
            filename="photo",
            modified=_NOW - timedelta(days=1),
        )
        v1 = _make_file(
            filename="photo (1)",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([base, v1])

        assert results[0].action == "skip"
        assert any("version" in r.lower() for r in results[0].reasons)
        assert results[0].duplicate_of == str(v1.path)
        assert results[1].action == "keep"


# ---------------------------------------------------------------------------
# Year vs counter disambiguation (issue #834)
# ---------------------------------------------------------------------------


class TestYearVsCounterDisambiguation:
    """Tests that 4-digit year suffixes are not treated as copy counters.

    Regression tests for issue #834: ``Taxes (2021).pdf`` and
    ``Taxes (2022).pdf`` are distinct annual documents, not versions of
    ``Taxes.pdf``, and must never be deduplicated against each other.
    Genuine 1-2 digit Drive copy counters must keep deduplicating.
    """

    def test_year_suffix_not_treated_as_version_duplicate(self) -> None:
        """Distinct annual documents like (2021)/(2022) must both be kept."""
        taxes_2021 = _make_file(
            filename="Taxes (2021).pdf",
            content="Tax return for fiscal year 2021 with unique figures.",
            modified=_NOW - timedelta(days=30),
        )
        taxes_2022 = _make_file(
            filename="Taxes (2022).pdf",
            content="Tax return for fiscal year 2022 with different figures.",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([taxes_2021, taxes_2022])

        assert results[0].action == "keep"
        assert results[1].action == "keep"
        for result in results:
            assert not any("version" in r.lower() for r in result.reasons)

    def test_year_suffix_documents_both_kept(self) -> None:
        """Neither year-suffixed document may be silently dropped."""
        taxes_2021 = _make_file(
            filename="Taxes (2021).pdf",
            content="Tax return for fiscal year 2021 with unique figures.",
            modified=_NOW - timedelta(days=30),
        )
        taxes_2022 = _make_file(
            filename="Taxes (2022).pdf",
            content="Tax return for fiscal year 2022 with different figures.",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([taxes_2021, taxes_2022])

        not_skipped = [r for r in results if r.action != "skip"]
        assert len(not_skipped) == 2
        assert results[0].duplicate_of is None
        assert results[1].duplicate_of is None

    def test_counter_after_year_strips_only_counter(self) -> None:
        """A copy counter after a year dedups against the year-bearing base.

        ``Taxes (2021) (1).pdf`` is a Drive copy of ``Taxes (2021).pdf``:
        only the trailing ``(1)`` counter is stripped, the year stays part
        of the base name, and the older year-bearing original is skipped
        in favour of the newer copy.
        """
        base = _make_file(
            filename="Taxes (2021).pdf",
            content="Tax return for fiscal year 2021, original upload.",
            modified=_NOW - timedelta(days=5),
        )
        copy = _make_file(
            filename="Taxes (2021) (1).pdf",
            content="Tax return for fiscal year 2021, re-uploaded copy.",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([base, copy])

        assert results[0].action == "skip"
        assert any("version" in r.lower() for r in results[0].reasons)
        assert results[0].duplicate_of == str(copy.path)
        assert results[1].action == "keep"


# ---------------------------------------------------------------------------
# Empty document filtering
# ---------------------------------------------------------------------------


class TestEmptyDocumentFiltering:
    """Tests for skipping empty or placeholder documents."""

    def test_empty_content_skipped(self) -> None:
        """Files with empty content should be skipped."""
        empty = _make_file(content="")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([empty])
        assert results[0].action == "skip"
        assert any("empty" in r.lower() for r in results[0].reasons)

    def test_whitespace_only_skipped(self) -> None:
        """Files with only whitespace should be skipped."""
        ws = _make_file(content="   \n\t\n   ")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([ws])
        assert results[0].action == "skip"

    def test_minimal_content_skipped(self) -> None:
        """Files with trivially short content should be skipped."""
        tiny = _make_file(content="Hi")
        gdf = GoogleDriveFilter(now=_NOW, min_content_length=10)

        results = gdf.filter_batch([tiny])
        assert results[0].action == "skip"
        assert any(
            "empty" in r.lower() or "short" in r.lower() for r in results[0].reasons
        )

    def test_substantial_content_kept(self) -> None:
        """Files with real content should not be filtered as empty."""
        doc = _make_file(
            content="This is a document with substantial meaningful content."
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        # Should not be skipped for empty reasons
        assert not any("empty" in r.lower() for r in results[0].reasons)


# ---------------------------------------------------------------------------
# Multi-author flagging
# ---------------------------------------------------------------------------


class TestMultiAuthorFlagging:
    """Tests for flagging multi-author documents."""

    def test_multi_author_flagged(self) -> None:
        """Documents with multiple authors should be flagged for review."""
        doc = _make_file(
            authors=["alice@example.com", "bob@example.com", "carol@example.com"],
            owner="alice@example.com",
        )
        gdf = GoogleDriveFilter(now=_NOW, multi_author_threshold=0.5)

        results = gdf.filter_batch([doc])
        assert results[0].action == "flag"
        assert any(
            "multi-author" in r.lower() or "author" in r.lower()
            for r in results[0].reasons
        )

    def test_single_author_not_flagged(self) -> None:
        """Single-author documents should not be flagged."""
        doc = _make_file(
            authors=["alice@example.com"],
            owner="alice@example.com",
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert not any("author" in r.lower() for r in results[0].reasons)

    def test_owner_dominant_not_flagged(self) -> None:
        """If owner is dominant contributor (below threshold), don't flag."""
        doc = _make_file(
            authors=[
                "alice@example.com",
                "alice@example.com",
                "alice@example.com",
                "bob@example.com",
            ],
            owner="alice@example.com",
        )
        # Non-owner contributions: 1/4 = 25%, below 50% threshold
        gdf = GoogleDriveFilter(now=_NOW, multi_author_threshold=0.5)

        results = gdf.filter_batch([doc])
        assert not any("author" in r.lower() for r in results[0].reasons)

    def test_configurable_threshold(self) -> None:
        """Multi-author threshold should be configurable."""
        doc = _make_file(
            authors=["alice@example.com", "alice@example.com", "bob@example.com"],
            owner="alice@example.com",
        )
        # Non-owner: 1/3 = 33%
        strict = GoogleDriveFilter(now=_NOW, multi_author_threshold=0.3)
        lenient = GoogleDriveFilter(now=_NOW, multi_author_threshold=0.5)

        strict_results = strict.filter_batch([doc])
        lenient_results = lenient.filter_batch([doc])

        assert any("author" in r.lower() for r in strict_results[0].reasons)
        assert not any("author" in r.lower() for r in lenient_results[0].reasons)

    def test_empty_authors_not_flagged(self) -> None:
        """Files with no author info should not be flagged for multi-author."""
        doc = StagedFile(
            path=Path("/staged/doc.docx"),
            filename="doc.docx",
            content="This is a real document with meaningful content.",
            modified=_NOW,
            authors=[],
            owner="alice@example.com",
            size_bytes=1024,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert not any("author" in r.lower() for r in results[0].reasons)


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------


class TestStalenessDetection:
    """Tests for flagging stale documents."""

    def test_stale_document_flagged(self) -> None:
        """Documents not modified in a long time should be flagged."""
        old = _make_file(modified=_NOW - timedelta(days=400))
        gdf = GoogleDriveFilter(now=_NOW, staleness_days=365)

        results = gdf.filter_batch([old])
        assert results[0].action == "flag"
        assert any("stale" in r.lower() for r in results[0].reasons)

    def test_recent_document_not_flagged(self) -> None:
        """Recently modified documents should not be flagged as stale."""
        recent = _make_file(modified=_NOW - timedelta(days=30))
        gdf = GoogleDriveFilter(now=_NOW, staleness_days=365)

        results = gdf.filter_batch([recent])
        assert not any("stale" in r.lower() for r in results[0].reasons)

    def test_configurable_staleness_days(self) -> None:
        """Staleness threshold should be configurable."""
        doc = _make_file(modified=_NOW - timedelta(days=100))

        short = GoogleDriveFilter(now=_NOW, staleness_days=90)
        long = GoogleDriveFilter(now=_NOW, staleness_days=180)

        short_results = short.filter_batch([doc])
        long_results = long.filter_batch([doc])

        assert any("stale" in r.lower() for r in short_results[0].reasons)
        assert not any("stale" in r.lower() for r in long_results[0].reasons)

    def test_boundary_staleness(self) -> None:
        """Document exactly at the staleness boundary should not be flagged."""
        boundary = _make_file(modified=_NOW - timedelta(days=365))
        gdf = GoogleDriveFilter(now=_NOW, staleness_days=365)

        results = gdf.filter_batch([boundary])
        assert not any("stale" in r.lower() for r in results[0].reasons)


# ---------------------------------------------------------------------------
# Cross-format deduplication
# ---------------------------------------------------------------------------


class TestCrossFormatDeduplication:
    """Tests for detecting identical content across file formats."""

    def test_docx_pdf_duplicate_detected(self) -> None:
        """Identical content in .docx and .pdf should flag the duplicate."""
        content = "This is the exact same document content for testing purposes."
        docx = _make_file(filename="report.docx", content=content, modified=_NOW)
        pdf = _make_file(
            filename="report.pdf",
            content=content,
            modified=_NOW - timedelta(hours=1),
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([docx, pdf])

        # One should be kept, the other skipped as cross-format duplicate
        actions = [r.action for r in results]
        assert "skip" in actions
        skipped_idx = actions.index("skip")
        assert results[skipped_idx].duplicate_of is not None
        assert any("cross-format" in r.lower() for r in results[skipped_idx].reasons)

    def test_different_content_different_formats_kept(self) -> None:
        """Different content in .docx and .pdf should both be kept."""
        docx = _make_file(filename="report.docx", content="Content A is unique.")
        pdf = _make_file(filename="budget.pdf", content="Content B is different.")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([docx, pdf])
        skip_reasons = [
            r for res in results for r in res.reasons if "cross-format" in r.lower()
        ]
        assert len(skip_reasons) == 0

    def test_cross_format_keeps_newest(self) -> None:
        """Cross-format dedup should keep the newest version."""
        content = "Identical cross-format content for dedup testing."
        older_pdf = _make_file(
            filename="doc.pdf",
            content=content,
            modified=_NOW - timedelta(days=10),
        )
        newer_docx = _make_file(
            filename="doc.docx",
            content=content,
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([older_pdf, newer_docx])

        # Newer docx should be kept, older pdf skipped
        assert results[0].action == "skip"  # older pdf
        assert results[1].action != "skip" or not any(
            "cross-format" in r.lower() for r in results[1].reasons
        )

    def test_same_format_not_cross_format_duplicate(self) -> None:
        """Same content in same format should not be cross-format duplicate."""
        content = "Same content here."
        doc1 = _make_file(filename="file1.docx", content=content)
        doc2 = _make_file(filename="file2.docx", content=content)
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc1, doc2])
        cross_format_reasons = [
            r for res in results for r in res.reasons if "cross-format" in r.lower()
        ]
        assert len(cross_format_reasons) == 0


# ---------------------------------------------------------------------------
# Batch filtering
# ---------------------------------------------------------------------------


class TestBatchFiltering:
    """Tests for batch filtering behaviour."""

    def test_empty_batch_returns_empty(self) -> None:
        """Empty input should return empty results."""
        gdf = GoogleDriveFilter(now=_NOW)
        results = gdf.filter_batch([])
        assert results == []

    def test_single_valid_file_kept(self) -> None:
        """A single valid file should be kept."""
        doc = _make_file()
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert len(results) == 1
        assert results[0].action == "keep"

    def test_results_match_input_order(self) -> None:
        """Results should be in the same order as input files."""
        files = [
            _make_file(filename="a.docx"),
            _make_file(filename="b.docx"),
            _make_file(filename="c.docx"),
        ]
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch(files)
        assert len(results) == 3

    def test_multiple_issues_combined(self) -> None:
        """A file with multiple issues should report all reasons."""
        stale_empty = _make_file(
            content="",
            modified=_NOW - timedelta(days=500),
        )
        gdf = GoogleDriveFilter(now=_NOW, staleness_days=365)

        results = gdf.filter_batch([stale_empty])
        # Should have at least the empty reason (skip takes priority)
        assert results[0].action == "skip"
        assert len(results[0].reasons) >= 1

    def test_mixed_batch(self) -> None:
        """Batch with valid, empty, stale, and duplicate files processes correctly."""
        valid = _make_file(filename="valid.docx")
        empty = _make_file(filename="empty.docx", content="")
        stale = _make_file(
            filename="old.docx",
            modified=_NOW - timedelta(days=500),
        )
        copy = _make_file(filename="Copy of valid.docx")

        gdf = GoogleDriveFilter(now=_NOW, staleness_days=365)

        results = gdf.filter_batch([valid, empty, stale, copy])
        assert results[0].action == "keep"  # valid
        assert results[1].action == "skip"  # empty
        assert results[2].action == "flag"  # stale
        assert results[3].action == "skip"  # copy of


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    """Tests for constructor configuration."""

    def test_default_configuration(self) -> None:
        """Default configuration should use sensible values."""
        gdf = GoogleDriveFilter(now=_NOW)
        assert gdf.staleness_days == 365
        assert gdf.multi_author_threshold == 0.5
        assert gdf.min_content_length == 10

    def test_custom_configuration(self) -> None:
        """Custom configuration should override defaults."""
        gdf = GoogleDriveFilter(
            now=_NOW,
            staleness_days=180,
            multi_author_threshold=0.3,
            min_content_length=50,
        )
        assert gdf.staleness_days == 180
        assert gdf.multi_author_threshold == 0.3
        assert gdf.min_content_length == 50


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and unusual inputs."""

    def test_unicode_filename(self) -> None:
        """Unicode filenames should be handled correctly."""
        doc = _make_file(filename="会議メモ.docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert len(results) == 1

    def test_copy_of_unicode(self) -> None:
        """'Copy of' detection should work with unicode filenames."""
        original = _make_file(filename="会議メモ.docx")
        copy = _make_file(filename="Copy of 会議メモ.docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([original, copy])
        assert results[1].action == "skip"

    def test_deeply_nested_path(self) -> None:
        """Files with deep paths should be handled."""
        doc = _make_file(filename="deep/nested/path/file.docx")
        doc = StagedFile(
            path=Path("/staged/deep/nested/path/file.docx"),
            filename="file.docx",
            content="Content here.",
            modified=_NOW,
            authors=["alice@example.com"],
            owner="alice@example.com",
            size_bytes=500,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert len(results) == 1

    def test_no_extension_file(self) -> None:
        """Files without extension should be handled for cross-format check."""
        doc = _make_file(filename="README")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert len(results) == 1

    def test_copy_of_in_middle_of_name_not_detected(self) -> None:
        """'Copy of' must be at the start of the filename."""
        doc = _make_file(filename="My Copy of Notes.docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert not any("copy of" in r.lower() for r in results[0].reasons)

    def test_version_group_ignores_already_skipped_files(self) -> None:
        """Version grouping should skip files already marked (e.g. Copy of)."""
        # "Copy of draft.docx" is skipped by copy-of check before
        # version grouping runs, so version scan of "draft (1).docx"
        # must ignore it when looking for the base "draft.docx"
        copy_as_base = _make_file(
            filename="Copy of draft.docx",
            modified=_NOW,
            content="",
        )
        versioned = _make_file(
            filename="draft (1).docx",
            modified=_NOW,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([copy_as_base, versioned])
        assert results[0].action == "skip"  # copy-of or empty
        assert len(results) == 2

    def test_version_suffix_without_original_in_batch(self) -> None:
        """A single versioned file without original should not cause errors."""
        versioned = _make_file(filename="report (3).docx")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([versioned])
        # Single file in version group — nothing to deduplicate
        assert len(results) == 1

    def test_cross_format_skips_empty_content_files(self) -> None:
        """Cross-format check should ignore files with empty content."""
        empty_docx = _make_file(filename="empty.docx", content="")
        empty_pdf = _make_file(filename="empty.pdf", content="")
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([empty_docx, empty_pdf])
        # Both should be skipped as empty, not as cross-format duplicates
        for r in results:
            assert r.action == "skip"
            assert not any("cross-format" in reason.lower() for reason in r.reasons)

    def test_cross_format_ignores_whitespace_only_content(self) -> None:
        """Cross-format dedup should skip files whose content is only whitespace."""
        ws_docx = _make_file(filename="blank.docx", content="   \n\t  ")
        ws_pdf = _make_file(filename="blank.pdf", content="   \n\t  ")
        # Use min_content_length=0 so whitespace files pass the empty check
        gdf = GoogleDriveFilter(now=_NOW, min_content_length=0)

        results = gdf.filter_batch([ws_docx, ws_pdf])
        # Should not be flagged as cross-format duplicates
        for r in results:
            assert not any("cross-format" in reason.lower() for reason in r.reasons)

    def test_multi_author_check_with_empty_authors_non_empty_content(self) -> None:
        """Multi-author check should handle empty authors on non-empty files."""
        doc = StagedFile(
            path=Path("/staged/doc.docx"),
            filename="doc.docx",
            content="Substantial document content here for testing.",
            modified=_NOW,
            authors=[],
            owner="alice@example.com",
            size_bytes=1024,
        )
        gdf = GoogleDriveFilter(now=_NOW)

        results = gdf.filter_batch([doc])
        assert not any("author" in r.lower() for r in results[0].reasons)
