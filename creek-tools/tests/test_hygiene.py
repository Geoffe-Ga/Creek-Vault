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
    aliases: list[str] | None = None,
) -> Path:
    """Write a fragment markdown file with frontmatter.

    Args:
        vault: Vault root path.
        name: Filename stem (without .md).
        content: Body content of the fragment.
        created: Optional created datetime for frontmatter.
        subfolder: Subfolder under 01-Fragments.
        aliases: Optional ``aliases`` entries — the extra names Obsidian
            lets the page be wiki-linked by.

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
    if aliases is not None:
        metadata["aliases"] = aliases
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

    def test_anchor_wikilink_counts_in_link_graph(self, tmp_path: Path) -> None:
        """An anchor wikilink connects both endpoints in the link graph (#835).

        Fragment ``a`` links to ``b`` only via ``[[b#Section]]``; the anchor
        must not hide the connection, so ``a`` has an outgoing link and ``b``
        has an incoming link — neither is an orphan.
        """
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        _write_fragment(vault, "b", created=old)
        _write_fragment(
            vault,
            "a",
            content="See [[b#Section]] for context.",
            created=old,
        )
        scanner = OrphanScanner(age_days=30)
        result = scanner.scan(vault)
        assert result.orphan_paths == []

    def test_inbound_alias_link_is_not_orphan(self, tmp_path: Path) -> None:
        """An alias-form inbound link saves a page from the orphan list (#1225).

        Since #730 the linkers write date-prefixed filenames and put the
        human-readable name in ``aliases``, so fragments link ``[[Messages]]``
        at a file called ``2020-09-26-messages.md``. Resolving against
        filename stems alone — the behaviour #887 removed from
        ``BrokenLinkScanner`` but not from here — calls that page an orphan
        and tells the operator to delete live content.

        The genuinely unreferenced ``lonely`` fragment must still be
        reported, so alias-awareness cannot be bought by falling silent.
        """
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        aliased = _write_fragment(
            vault,
            "2020-09-26-messages",
            created=old,
            aliases=["Messages"],
        )
        _write_fragment(
            vault,
            "referrer",
            content="Filed under [[Messages]].",
            created=old,
        )
        lonely = _write_fragment(vault, "lonely", created=old)

        result = OrphanScanner(age_days=30).scan(vault)

        assert str(aliased) not in result.orphan_paths
        assert result.orphan_paths == [str(lonely)]

    def test_outbound_alias_link_is_not_orphan(self, tmp_path: Path) -> None:
        """A fragment linking a compiled page by alias has outgoing links.

        The thread page's filename carries a date prefix; the fragment links
        the alias. Stem-only matching found no such target and therefore
        credited the fragment with no outgoing links at all.
        """
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        thread = vault / "02-Threads" / "Active" / "2020-09-26-messages.md"
        _write_md_file(
            thread,
            "---\naliases:\n  - Messages\n---\n\nThread body.\n",
        )
        _write_fragment(
            vault,
            "linker",
            content="Belongs to [[Messages]].",
            created=old,
        )

        result = OrphanScanner(age_days=30).scan(vault)

        assert result.orphan_paths == []

    def test_alias_resolution_credits_only_the_named_page(
        self,
        tmp_path: Path,
    ) -> None:
        """Resolution is per-page: a resolving link credits one page only.

        ``referrer`` names ``beta`` by alias. ``alpha`` also declares an
        alias but nothing links it, so it stays orphaned — a check that
        credited every aliased page as soon as *some* link resolved would
        fall silent, which is the same end state as the bug being fixed.
        """
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        alpha = _write_fragment(vault, "alpha", created=old, aliases=["Alpha Page"])
        _write_fragment(vault, "beta", created=old, aliases=["Beta Page"])
        _write_fragment(
            vault,
            "referrer",
            content="See [[Beta Page]].",
            created=old,
        )

        result = OrphanScanner(age_days=30).scan(vault)

        assert result.orphan_paths == [str(alpha)]

    def test_self_link_does_not_rescue_a_fragment(self, tmp_path: Path) -> None:
        """A fragment whose only link points at itself is still an orphan.

        Outgoing links already excluded self-references; incoming links did
        not, so ``[[selfref]]`` inside ``selfref.md`` silently exempted the
        page. Per-page resolution makes both directions agree.
        """
        vault = _make_vault(tmp_path)
        old = datetime.now(tz=UTC) - timedelta(days=60)
        selfref = _write_fragment(
            vault,
            "selfref",
            content="Written about in [[selfref]].",
            created=old,
        )

        result = OrphanScanner(age_days=30).scan(vault)

        assert result.orphan_paths == [str(selfref)]


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

    def test_a_short_time_run_is_rejected_not_re_segmented(
        self,
        tmp_path: Path,
    ) -> None:
        """A 4-digit time must not parse as a wrong-but-plausible time.

        ``%H%M%S`` is unseparated, so ``strptime`` let each directive match
        one *or* two digits and read ``2024-03-15_0830`` as 08:**03**:00 —
        aging the queue by 27 minutes it never had, with no signal. Same
        re-segmentation defect as the PDF date parser's (#1632).

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        scanner = StaleReviewScanner(age_days=0)
        short = tmp_path / "review-queue-2024-03-15_0830.md"
        full = tmp_path / "review-queue-2024-03-15_083000.md"

        assert scanner._parse_filename_timestamp(short) is None, (
            "a 4-digit time run was re-segmented into a wrong time instead "
            "of being rejected"
        )
        assert scanner._parse_filename_timestamp(full) == datetime(
            2024, 3, 15, 8, 30, tzinfo=UTC
        )


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

    def test_heading_anchor_wikilink_not_broken(self, tmp_path: Path) -> None:
        """A ``[[b#Section]]`` link to an existing file is not broken (#835)."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "b", content="Target with a heading.")
        _write_fragment(vault, "a", content="See [[b#Section]] for details.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_same_file_anchor_wikilink_not_broken(self, tmp_path: Path) -> None:
        """A same-file anchor link like ``[[#Heading]]`` is not broken (#835)."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "self-anchor", content="Jump to [[#Heading]] above.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_heading_anchor_wikilink_with_alias_not_broken(
        self,
        tmp_path: Path,
    ) -> None:
        """A ``[[b#Section|display]]`` link to an existing file is not broken (#835)."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "b", content="Target with a heading.")
        _write_fragment(vault, "a", content="See [[b#Section|display]] here.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_relative_link_with_anchor_not_broken(self, tmp_path: Path) -> None:
        """A relative link with a heading anchor to an existing file is fine (#835)."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "b", content="Target content.")
        _write_fragment(vault, "a", content="See [text](b.md#Section) for more.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 0

    def test_missing_target_with_anchor_still_broken(self, tmp_path: Path) -> None:
        """A ``[[missing#Section]]`` link to no file reports ``[[missing]]`` (#835)."""
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "a", content="See [[missing#Section]] here.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 1
        broken_targets = next(iter(result.broken_links.values()))
        assert broken_targets == ["[[missing]]"]

    def test_scan_covers_non_fragment_sources(self, tmp_path: Path) -> None:
        """Threads, praxis and decisions carry links too (#1344).

        The scanner surveyed ``01-Fragments`` alone while ``creek lint``
        reported its count as a whole-vault verdict, so a dangling link on a
        compiled page was invisible no matter how many times it ran.
        """
        vault = _make_vault(tmp_path)
        source = _write_md_file(
            vault / "02-Threads" / "Active" / "t.md",
            "See [[ghost-thread]] here.",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_broken == 1
        assert result.broken_links[str(source)] == ["[[ghost-thread]]"]

    def test_total_files_scanned_counts_every_surveyed_file(
        self,
        tmp_path: Path,
    ) -> None:
        """The count reports the survey, which is no longer fragments-only.

        ``creek lint`` renders this number as "across N file(s)", so leaving
        it at the fragment count would keep the verdict overstating its own
        coverage even after the survey widened (#1344).
        """
        vault = _make_vault(tmp_path)
        _write_fragment(vault, "f1", content="No links at all.")
        _write_md_file(vault / "02-Threads" / "Active" / "t.md", "Thread body.")
        _write_md_file(vault / "00-Creek-Meta" / "Tag-Garden.md", "Garden body.")
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.total_files_scanned == 3
        assert result.total_broken == 0

    def test_scan_skips_creek_report_directories(self, tmp_path: Path) -> None:
        """Creek's own reports echo findings; reading them back inflates them.

        ``lint-<date>.md`` renders each finding as ``- `src` → `[[target]]` ``
        and ``State/latest.md`` echoes the same lines, so a literal
        whole-vault survey grew one genuine broken link into 1, then 2, then 3
        over three successive runs (#1344).
        """
        vault = _make_vault(tmp_path)
        _write_md_file(
            vault / "00-Creek-Meta" / "Processing-Log" / "lint-2026-08-09.md",
            "- `01-Fragments/Conversations/a.md` → `[[ghost]]`\n",
        )
        _write_md_file(
            vault / "00-Creek-Meta" / "State" / "latest.md",
            "- Broken links in `x`: [[phantom]]\n",
        )
        scanner = BrokenLinkScanner()
        result = scanner.scan(vault)
        assert result.broken_links == {}
        assert result.total_broken == 0
        assert result.total_files_scanned == 0


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

    def test_extract_wikilinks_strips_heading_anchor(self) -> None:
        """Heading anchors are stripped from wiki-link targets (#835).

        ``[[b#Section]]`` yields ``b``, ``[[Note#H|alias]]`` yields ``Note``,
        and a same-file anchor like ``[[#Self]]`` yields no target at all.
        """
        from creek.clean.hygiene import _extract_wikilinks

        content = "See [[b#Section]] and [[Note#H|alias]] and [[#Self]]."
        assert _extract_wikilinks(content) == ["b", "Note"]
        # A '#' inside the alias (after the pipe) must not affect the target,
        # and a nested heading path keeps only the file portion.
        assert _extract_wikilinks("[[Target|Display#thing]]") == ["Target"]
        assert _extract_wikilinks("[[Note#H1#H2]]") == ["Note"]

    def test_extract_relative_links_strips_anchor_and_query(self) -> None:
        """Anchor and query suffixes are stripped from relative targets (#835).

        The URL token is cut at the first ``#`` or ``?``; a target that is
        empty after stripping (query-only) is skipped entirely.
        """
        from creek.clean.hygiene import _extract_relative_links

        content = "[x](b.md#Section) and [y](c.md?raw=1)"
        assert _extract_relative_links(content) == ["b.md", "c.md"]
        assert _extract_relative_links("[q](?query)") == []
