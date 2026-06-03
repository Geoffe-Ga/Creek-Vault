"""Tests for vault hygiene scanners — orphans, stale reviews, broken links, duplicates.

Verifies that the hygiene scanners correctly identify orphaned fragments,
stale review queue files, broken wiki-links and relative links, duplicate
content, and generate accurate health reports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter

from creek.clean.hygiene import (
    BrokenLinkScanner,
    DuplicateScanner,
    HygieneReporter,
    OrphanScanner,
    StaleReviewScanner,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    """Create a minimal vault structure for testing.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the vault root.
    """
    vault = tmp_path / "vault"
    for d in [
        "00-Creek-Meta",
        "01-Fragments/Conversations",
        "02-Threads/Active",
        "03-Eddies",
    ]:
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _write_fragment(
    vault: Path,
    name: str,
    content: str = "Some meaningful content here.",
    *,
    created: datetime | None = None,
    subfolder: str = "Conversations",
) -> Path:
    """Write a fragment markdown file with frontmatter.

    Args:
        vault: Vault root path.
        name: Filename stem (without .md).
        content: Body content of the fragment.
        created: Optional created datetime for frontmatter.
        subfolder: Subfolder under 01-Fragments.

    Returns:
        Path to the written file.
    """
    target = vault / "01-Fragments" / subfolder / f"{name}.md"
    metadata: dict[str, object] = {
        "id": f"frag-{name}",
        "title": name,
        "type": "fragment",
        "source": {"platform": "claude", "original_file": f"{name}.json"},
    }
    if created is not None:
        metadata["created"] = created.isoformat()
    post = frontmatter.Post(content=content, **metadata)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_md_file(
    path: Path,
    content: str = "",
) -> Path:
    """Write a plain markdown file (no frontmatter).

    Args:
        path: Full file path.
        content: File body content.

    Returns:
        Path to the written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# OrphanScanner tests
# ---------------------------------------------------------------------------


class TestOrphanScanner:
    """Tests for OrphanScanner."""

    def test_no_fragments_returns_empty(self, tmp_path: Path) -> None:
        """Empty vault yields zero orphans."""
        vault = _make_vault(tmp_path)
        scanner = OrphanScanner(age_days=0)
        result = scanner.scan(vault)
        assert result.orphan_paths == []
        assert result.total_fragments == 0

    def test_linked_fragment_not_orphan(self, tmp_path: Path) -> None:
        """A fragment linked from another file is not an orphan."""
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        _write_fragment(vault, "alpha", created=old)
        _write_fragment(
            vault,
            "beta",
            content="See [[alpha]] for context.",
            created=old,
        )
        scanner = OrphanScanner(age_days=30)
        result = scanner.scan(vault)
        orphan_stems = [p.split("/")[-1] for p in result.orphan_paths]
        assert "alpha.md" not in orphan_stems

    def test_unlinked_old_fragment_is_orphan(self, tmp_path: Path) -> None:
        """An old fragment with no links is detected as orphan."""
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        _write_fragment(vault, "lonely", created=old)
        scanner = OrphanScanner(age_days=30)
        result = scanner.scan(vault)
        assert len(result.orphan_paths) == 1
        assert "lonely.md" in result.orphan_paths[0]

    def test_young_unlinked_fragment_not_orphan(self, tmp_path: Path) -> None:
        """A recent fragment is not flagged even without links."""
        vault = _make_vault(tmp_path)
        recent = datetime.now(tz=UTC) - timedelta(days=1)
        _write_fragment(vault, "new-frag", created=recent)
        scanner = OrphanScanner(age_days=30)
        result = scanner.scan(vault)
        assert len(result.orphan_paths) == 0

    def test_fragment_with_outgoing_link_not_orphan(self, tmp_path: Path) -> None:
        """A fragment that links outward to a known file is not an orphan."""
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        _write_md_file(vault / "02-Threads" / "Active" / "thread-a.md", "Thread A")
        _write_fragment(
            vault,
            "linker",
            content="Connects to [[thread-a]].",
            created=old,
        )
        scanner = OrphanScanner(age_days=30)
        result = scanner.scan(vault)
        assert len(result.orphan_paths) == 0

    def test_total_fragments_count(self, tmp_path: Path) -> None:
        """Total fragments count matches actual files."""
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        _write_fragment(vault, "a", created=old)
        _write_fragment(vault, "b", created=old)
        _write_fragment(vault, "c", created=old)
        scanner = OrphanScanner(age_days=30)
        result = scanner.scan(vault)
        assert result.total_fragments == 3

    def test_missing_fragments_dir(self, tmp_path: Path) -> None:
        """Vault without 01-Fragments directory returns empty result."""
        vault = tmp_path / "empty-vault"
        vault.mkdir()
        scanner = OrphanScanner(age_days=0)
        result = scanner.scan(vault)
        assert result.total_fragments == 0

    def test_custom_age_threshold(self, tmp_path: Path) -> None:
        """Custom age_days threshold is respected."""
        vault = _make_vault(tmp_path)
        medium_old = datetime.now(tz=UTC) - timedelta(days=10)
        _write_fragment(vault, "medium", created=medium_old)
        scanner_strict = OrphanScanner(age_days=5)
        result_strict = scanner_strict.scan(vault)
        assert len(result_strict.orphan_paths) == 1

        scanner_loose = OrphanScanner(age_days=20)
        result_loose = scanner_loose.scan(vault)
        assert len(result_loose.orphan_paths) == 0


# ---------------------------------------------------------------------------
# StaleReviewScanner tests
# ---------------------------------------------------------------------------


class TestStaleReviewScanner:
    """Tests for StaleReviewScanner."""

    def test_no_review_files(self, tmp_path: Path) -> None:
        """Vault with no review files returns empty result."""
        vault = _make_vault(tmp_path)
        scanner = StaleReviewScanner(age_days=14)
        result = scanner.scan(vault)
        assert result.stale_paths == []
        assert result.total_review_files == 0

    def test_stale_review_detected(self, tmp_path: Path) -> None:
        """An old review queue file is flagged as stale."""
        vault = _make_vault(tmp_path)
        old_date = datetime.now(tz=UTC) - timedelta(days=30)
        filename = f"review-queue-{old_date.strftime('%Y-%m-%d_%H%M%S')}.md"
        _write_md_file(vault / filename, "# Old Review Queue")
        scanner = StaleReviewScanner(age_days=14)
        result = scanner.scan(vault)
        assert len(result.stale_paths) == 1
        assert result.total_review_files == 1

    def test_fresh_review_not_stale(self, tmp_path: Path) -> None:
        """A recent review file is not flagged as stale."""
        vault = _make_vault(tmp_path)
        recent = datetime.now(tz=UTC) - timedelta(days=1)
        filename = f"review-queue-{recent.strftime('%Y-%m-%d_%H%M%S')}.md"
        _write_md_file(vault / filename, "# Fresh Review Queue")
        scanner = StaleReviewScanner(age_days=14)
        result = scanner.scan(vault)
        assert len(result.stale_paths) == 0
        assert result.total_review_files == 1

    def test_custom_age_threshold(self, tmp_path: Path) -> None:
        """Custom age_days threshold is respected."""
        vault = _make_vault(tmp_path)
        medium = datetime.now(tz=UTC) - timedelta(days=5)
        filename = f"review-queue-{medium.strftime('%Y-%m-%d_%H%M%S')}.md"
        _write_md_file(vault / filename, "# Medium Review Queue")

        scanner_strict = StaleReviewScanner(age_days=3)
        assert len(scanner_strict.scan(vault).stale_paths) == 1

        scanner_loose = StaleReviewScanner(age_days=10)
        assert len(scanner_loose.scan(vault).stale_paths) == 0

    def test_malformed_filename_falls_back_to_mtime(self, tmp_path: Path) -> None:
        """Review file with bad timestamp falls back to file mtime."""
        vault = _make_vault(tmp_path)
        _write_md_file(vault / "review-queue-bad-date.md", "# Bad date")
        scanner = StaleReviewScanner(age_days=0)
        result = scanner.scan(vault)
        assert result.total_review_files == 1


# ---------------------------------------------------------------------------
# BrokenLinkScanner tests
# ---------------------------------------------------------------------------


class TestBrokenLinkScanner:
    """Tests for BrokenLinkScanner."""

    def test_no_broken_links(self, tmp_path: Path) -> None:
        """Vault with valid links reports zero broken."""
        vault = _make_vault(tmp_path)
        _write_md_file(vault / "02-Threads" / "Active" / "target.md", "Target")
        _write_fragment(vault, "source", content="See [[target]] for details.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_broken_wikilink_detected(self, tmp_path: Path) -> None:
        """A wikilink to a nonexistent file is reported."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "broken", content="See [[nonexistent]] here.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 1
        broken_targets = next(iter(result.broken_links.values()))
        assert "[[nonexistent]]" in broken_targets

    def test_broken_relative_link_detected(self, tmp_path: Path) -> None:
        """A relative markdown link to a missing file is reported."""
        vault = _make_vault(tmp_path)
        _write_fragment(
            vault,
            "rel-broken",
            content="See [link](missing-file.md) for more.",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 1

    def test_valid_relative_link_not_broken(self, tmp_path: Path) -> None:
        """A relative link to an existing file is not reported."""
        vault = _make_vault(tmp_path)
        sibling = vault / "01-Fragments" / "Conversations" / "sibling.md"
        _write_md_file(sibling, "Sibling content")
        _write_fragment(
            vault,
            "has-rel",
            content="See [link](sibling.md) for more.",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_wikilink_with_alias(self, tmp_path: Path) -> None:
        """A wikilink with alias like [[Target|Display]] checks Target."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "alias-test", content="See [[missing|alias]] here.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 1

    def test_empty_vault_no_errors(self, tmp_path: Path) -> None:
        """Scanning an empty vault produces zero broken links."""
        vault = _make_vault(tmp_path)
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0
        assert result.total_files_scanned == 0

    def test_http_links_ignored(self, tmp_path: Path) -> None:
        """HTTP(S) links are not treated as relative links."""
        vault = _make_vault(tmp_path)
        _write_fragment(
            vault,
            "web",
            content="Visit [site](https://example.com) for more.",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_multiple_broken_in_one_file(self, tmp_path: Path) -> None:
        """Multiple broken links in one file are all reported."""
        vault = _make_vault(tmp_path)
        _write_fragment(
            vault,
            "multi",
            content="See [[ghost1]] and [[ghost2]] and [[ghost3]].",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 3

    def test_long_absolute_image_url_with_title_not_broken(
        self,
        tmp_path: Path,
    ) -> None:
        """A long absolute image URL with a title does not crash or report (#543)."""
        vault = _make_vault(tmp_path)
        long_url = "https://substackcdn.com/image/fetch/" + "a" * 4000 + ".png"
        _write_fragment(
            vault,
            "substack-image",
            content=f'![alt text]({long_url} "an image title")',
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_protocol_relative_url_ignored(self, tmp_path: Path) -> None:
        """Protocol-relative (``//host/...``) URLs are not treated as local (#543)."""
        vault = _make_vault(tmp_path)
        long_target = "//substackcdn.com/image/" + "b" * 5000 + ".png"
        _write_fragment(
            vault,
            "proto-rel",
            content=f"![img]({long_target})",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_mailto_link_ignored(self, tmp_path: Path) -> None:
        """``mailto:`` links are external and not reported as broken (#543)."""
        vault = _make_vault(tmp_path)
        _write_fragment(
            vault,
            "mail",
            content="Reach me at [email](mailto:geoff@example.com).",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_pathological_relative_target_does_not_crash(
        self,
        tmp_path: Path,
    ) -> None:
        """An over-long relative file target degrades gracefully (#543).

        A target with no URL scheme but an over-long path segment would
        raise ``OSError`` from ``Path.exists()``; the scan must not crash.
        """
        vault = _make_vault(tmp_path)
        long_name = "c" * 5000 + ".md"
        _write_fragment(
            vault,
            "pathological",
            content=f"See [doc]({long_name}) for more.",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        # Must not raise; the unresolvable target is treated as not-a-file.
        assert result.total_broken == 0


# ---------------------------------------------------------------------------
# DuplicateScanner tests
# ---------------------------------------------------------------------------


class TestDuplicateScanner:
    """Tests for DuplicateScanner."""

    def test_no_duplicates(self, tmp_path: Path) -> None:
        """Unique fragments yield no duplicate candidates."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "unique-a", content="First unique content.")
        _write_fragment(vault, "unique-b", content="Second unique content.")
        scanner = DuplicateScanner()
        result = scanner.scan(vault)
        assert len(result.candidates) == 0
        assert result.total_fragments == 2

    def test_exact_duplicate_detected(self, tmp_path: Path) -> None:
        """Two fragments with identical content are detected."""
        vault = _make_vault(tmp_path)
        now = datetime.now(tz=UTC)
        _write_fragment(vault, "dup-a", content="Exactly the same text.", created=now)
        _write_fragment(vault, "dup-b", content="Exactly the same text.", created=now)
        scanner = DuplicateScanner()
        result = scanner.scan(vault)
        assert len(result.candidates) >= 1

    def test_empty_vault(self, tmp_path: Path) -> None:
        """Empty vault returns empty result."""
        vault = _make_vault(tmp_path)
        scanner = DuplicateScanner()
        result = scanner.scan(vault)
        assert result.total_fragments == 0
        assert len(result.candidates) == 0

    def test_empty_content_skipped(self, tmp_path: Path) -> None:
        """Fragments with empty body content are skipped."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "empty-body", content="")
        _write_fragment(vault, "has-body", content="Real content here.")
        scanner = DuplicateScanner()
        result = scanner.scan(vault)
        assert len(result.candidates) == 0

    def test_non_dict_source_falls_back_to_file_path(
        self,
        tmp_path: Path,
    ) -> None:
        """A fragment whose `source` is a scalar reports the file path."""
        vault = _make_vault(tmp_path)
        frag = vault / "01-Fragments" / "Conversations" / "scalar-source.md"
        frag.parent.mkdir(parents=True, exist_ok=True)
        frag.write_text(
            "---\nid: frag-x\ntitle: X\ntype: fragment\n"
            'source: "legacy.txt"\n---\n\nBody content here.\n',
            encoding="utf-8",
        )
        content, source, _ = DuplicateScanner()._read_fragment(frag)
        assert content == "Body content here."
        assert source == str(frag)


# ---------------------------------------------------------------------------
# HygieneReporter tests
# ---------------------------------------------------------------------------


class TestHygieneReporter:
    """Tests for HygieneReporter."""

    def test_empty_vault_report(self, tmp_path: Path) -> None:
        """Report on empty vault has all zeros."""
        vault = _make_vault(tmp_path)
        reporter = HygieneReporter()
        report = reporter.generate(vault)
        assert report.total_fragments == 0
        assert report.orphan_count == 0
        assert report.stale_review_count == 0
        assert report.broken_link_count == 0
        assert report.duplicate_candidate_count == 0

    def test_report_counts_orphans(self, tmp_path: Path) -> None:
        """Report reflects orphan count."""
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        _write_fragment(vault, "orphan", created=old)
        reporter = HygieneReporter(orphan_age_days=30)
        report = reporter.generate(vault)
        assert report.orphan_count == 1
        assert report.total_fragments == 1

    def test_report_counts_broken_links(self, tmp_path: Path) -> None:
        """Report reflects broken link count."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "bad-links", content="See [[missing]].")
        reporter = HygieneReporter()
        report = reporter.generate(vault)
        assert report.broken_link_count == 1

    def test_report_counts_stale_reviews(self, tmp_path: Path) -> None:
        """Report reflects stale review file count."""
        vault = _make_vault(tmp_path)
        old_date = datetime.now(tz=UTC) - timedelta(days=30)
        filename = f"review-queue-{old_date.strftime('%Y-%m-%d_%H%M%S')}.md"
        _write_md_file(vault / filename, "# Stale")
        reporter = HygieneReporter(stale_review_age_days=14)
        report = reporter.generate(vault)
        assert report.stale_review_count == 1

    def test_quality_distribution(self, tmp_path: Path) -> None:
        """Report computes quality distribution across fragments."""
        vault = _make_vault(tmp_path)
        _write_fragment(
            vault,
            "quality-test",
            content="This is a meaningful fragment with sufficient length and variety.",
        )
        reporter = HygieneReporter()
        report = reporter.generate(vault)
        total = sum(report.quality_distribution.values())
        assert total == 1

    def test_write_markdown_report(self, tmp_path: Path) -> None:
        """Markdown report is written to the specified path."""
        vault = _make_vault(tmp_path)
        reporter = HygieneReporter()
        report = reporter.generate(vault)
        output = tmp_path / "report.md"
        result_path = reporter.write_markdown(report, output)
        assert result_path.exists()
        content = result_path.read_text(encoding="utf-8")
        assert "Vault Hygiene Report" in content
        assert "Total fragments" in content

    def test_write_markdown_includes_quality(self, tmp_path: Path) -> None:
        """Markdown report includes quality distribution table."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "q-frag", content="Content for quality scoring test.")
        reporter = HygieneReporter()
        report = reporter.generate(vault)
        output = tmp_path / "report-q.md"
        reporter.write_markdown(report, output)
        content = output.read_text(encoding="utf-8")
        assert "Quality Distribution" in content


# ---------------------------------------------------------------------------
# Link extraction tests
# ---------------------------------------------------------------------------


class TestLinkExtraction:
    """Tests for wiki-link and relative link extraction helpers."""

    def test_extract_wikilinks(self) -> None:
        """Wiki-links are correctly extracted."""
        from creek.clean.hygiene import _extract_wikilinks

        content = "See [[Alpha]] and [[Beta|display]] for details."
        links = _extract_wikilinks(content)
        assert "Alpha" in links
        assert "Beta" in links
        assert len(links) == 2

    def test_extract_relative_links(self) -> None:
        """Relative markdown links are correctly extracted."""
        from creek.clean.hygiene import _extract_relative_links

        content = "See [link](path/to/file.md) and [web](https://example.com)."
        links = _extract_relative_links(content)
        assert "path/to/file.md" in links
        assert len(links) == 1

    def test_extract_wikilinks_empty(self) -> None:
        """No wiki-links in plain text."""
        from creek.clean.hygiene import _extract_wikilinks

        assert _extract_wikilinks("No links here.") == []

    def test_extract_relative_links_anchor_ignored(self) -> None:
        """Anchor-only links are ignored."""
        from creek.clean.hygiene import _extract_relative_links

        content = "See [section](#heading) for details."
        assert _extract_relative_links(content) == []

    def test_extract_relative_links_protocol_relative_ignored(self) -> None:
        """Protocol-relative (``//host``) targets are excluded (#543)."""
        from creek.clean.hygiene import _extract_relative_links

        content = "![img](//cdn.example.com/x.png)"
        assert _extract_relative_links(content) == []

    def test_extract_relative_links_mailto_ignored(self) -> None:
        """``mailto:`` targets are excluded (#543)."""
        from creek.clean.hygiene import _extract_relative_links

        content = "[mail](mailto:foo@bar.com)"
        assert _extract_relative_links(content) == []

    def test_extract_relative_links_scheme_with_leading_space_ignored(
        self,
    ) -> None:
        """A scheme target with leading whitespace is still excluded (#543)."""
        from creek.clean.hygiene import _extract_relative_links

        content = "[x]( https://example.com/y)"
        assert _extract_relative_links(content) == []
