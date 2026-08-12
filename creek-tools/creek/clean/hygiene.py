"""Vault hygiene scanners — orphan detection, stale reviews, broken links, and reports.

Provides scanner classes that analyse an Obsidian vault for common
hygiene issues:

- **OrphanScanner**: Finds fragments with zero incoming or outgoing links
  after a configurable age threshold.
- **StaleReviewScanner**: Finds review queue items older than a threshold.
- **BrokenLinkScanner**: Finds wiki-links and relative links that point
  to nonexistent files.
- **DuplicateScanner**: Runs normalized + semantic dedup sweep and outputs
  a review report.
- **HygieneReporter**: Generates a summary report of vault health.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
from pydantic import BaseModel, Field

from creek.vault.links import build_link_index, iter_link_sources

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.vault.links import LinkIndex


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class OrphanResult(BaseModel):
    """Result of an orphan scan.

    Attributes:
        orphan_paths: Paths to fragments identified as orphans.
        total_fragments: Total number of fragments scanned.
    """

    orphan_paths: list[str] = Field(default_factory=list)
    total_fragments: int = 0


class StaleReviewResult(BaseModel):
    """Result of a stale review scan.

    Attributes:
        stale_paths: Paths to stale review queue files.
        total_review_files: Total review files scanned.
    """

    stale_paths: list[str] = Field(default_factory=list)
    total_review_files: int = 0


class BrokenLinkResult(BaseModel):
    """Result of a broken link scan.

    Attributes:
        broken_links: Mapping of source file path to list of broken targets.
        total_files_scanned: Total markdown files scanned.
        total_broken: Total count of broken links found.
    """

    broken_links: dict[str, list[str]] = Field(default_factory=dict)
    total_files_scanned: int = 0
    total_broken: int = 0


class DuplicateCandidate(BaseModel):
    """A pair of files identified as potential duplicates.

    Attributes:
        file_a: Path to the first file.
        file_b: Path to the second file.
        match_type: Type of match (``exact`` or ``normalized``).
    """

    file_a: str
    file_b: str
    match_type: str


class DuplicateResult(BaseModel):
    """Result of a duplicate scan.

    Attributes:
        candidates: List of duplicate candidate pairs.
        total_fragments: Total fragments scanned.
    """

    candidates: list[DuplicateCandidate] = Field(default_factory=list)
    total_fragments: int = 0


class HygieneReport(BaseModel):
    """Summary report of vault health.

    Attributes:
        total_fragments: Total fragment files in the vault.
        orphan_count: Number of orphaned fragments.
        stale_review_count: Number of stale review files.
        broken_link_count: Number of broken links.
        duplicate_candidate_count: Number of duplicate candidates.
        quality_distribution: Mapping of quality action to count.
    """

    total_fragments: int = 0
    orphan_count: int = 0
    stale_review_count: int = 0
    broken_link_count: int = 0
    duplicate_candidate_count: int = 0
    quality_distribution: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Link extraction patterns
# ---------------------------------------------------------------------------


def _parse_datetime(value: object) -> datetime | None:
    """Parse a value into a datetime, handling strings and datetime objects.

    Args:
        value: A datetime, ISO-format string, or other value.

    Returns:
        Parsed datetime, or None if parsing fails.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


_WIKILINK_PATTERN: re.Pattern[str] = re.compile(
    r"\[\[([^\]|#]+?)(?:[#|][^\]]*?)?\]\]",
)
"""Matches Obsidian wiki-links, capturing only the file portion of the target.

Heading anchors (``[[Note#Heading]]``) and aliases (``[[Note|alias]]``) are
excluded from the captured target, and same-file anchors (``[[#Heading]]``)
produce no target at all — they name no file to resolve (issue #835).
"""

_RELATIVE_LINK_PATTERN: re.Pattern[str] = re.compile(
    r"\[([^\]]*)\]\((?!#)([^)]+)\)",
)
"""Matches markdown links like ``[text](path/to/file.md)`` (excluding anchors)."""

_URL_SCHEME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
"""Matches a leading URI scheme such as ``http:``, ``https:``, or ``mailto:``."""


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def _list_fragment_files(vault_path: Path) -> list[Path]:
    """List all markdown files under the 01-Fragments directory.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        Sorted list of ``.md`` file paths.
    """
    fragments_dir = vault_path / "01-Fragments"
    if not fragments_dir.exists():
        return []
    return sorted(fragments_dir.rglob("*.md"))


def _list_all_md_files(vault_path: Path) -> list[Path]:
    """List all markdown files in the vault.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        Sorted list of ``.md`` file paths.
    """
    return sorted(vault_path.rglob("*.md"))


def _extract_wikilinks(content: str) -> list[str]:
    """Extract wiki-link targets from markdown content.

    Targets are the file portions only: heading anchors and aliases are
    stripped by the pattern, and same-file anchor links (``[[#Heading]]``)
    yield no target (issue #835).

    Args:
        content: Raw markdown text.

    Returns:
        List of wiki-link target strings (file portions only).
    """
    return _WIKILINK_PATTERN.findall(content)


def _is_external_target(target: str) -> bool:
    """Determine whether a link target points outside the local filesystem.

    External targets are out of scope for a broken *local file* link scan:
    absolute URLs with a scheme (``https://``, ``mailto:``, ``ftp://``, ...)
    and protocol-relative URLs (``//host/path``).

    Args:
        target: A whitespace-stripped link target.

    Returns:
        True if the target is an external URL rather than a local path.
    """
    return target.startswith("//") or bool(_URL_SCHEME_PATTERN.match(target))


def _extract_relative_links(content: str) -> list[str]:
    """Extract relative (local file) link targets from markdown content.

    The raw target captured by the pattern may carry an optional title
    suffix (``path "title"``); only the URL portion is considered. Anchor
    (``#section``) and query (``?raw=1``) suffixes are stripped — Obsidian
    links like ``[text](file.md#section)`` target the file portion — and a
    target that is empty after stripping names no file, so it is skipped
    (issue #835). External targets (URLs with a scheme, or protocol-relative
    ``//host`` targets) are excluded, since they are not local file
    references.

    Args:
        content: Raw markdown text.

    Returns:
        List of relative link target strings (file portions only), with
        external URLs removed.
    """
    targets: list[str] = []
    for match in _RELATIVE_LINK_PATTERN.findall(content):
        # A markdown link target may be ``path "optional title"``; the URL
        # is the first whitespace-delimited token, cut at any anchor/query.
        target = match[1].strip().split(maxsplit=1)[0] if match[1].strip() else ""
        target = target.partition("#")[0].partition("?")[0]
        if not target or _is_external_target(target):
            continue
        targets.append(target)
    return targets


class OrphanScanner:
    """Identify fragments with zero incoming or outgoing links.

    Scans all fragment files and builds a link graph. Fragments with
    no connections after a configurable age threshold (in days) are
    reported as orphans.

    Wiki-links resolve through :func:`creek.vault.links.build_link_index`,
    the same index :class:`BrokenLinkScanner` and the ``orphan-compiled``
    lint check use, so a page is credited for links naming it by its
    frontmatter ``title`` or any ``aliases`` entry. Comparing filename
    stems alone — the behaviour #887 removed from the lint checks and
    #1225 removed from here — reported alias-linked pages as orphans,
    which is a false positive telling the operator to delete live content.

    Attributes:
        age_days: Minimum age in days before a fragment is considered
            an orphan candidate (default 30).
    """

    def __init__(self, *, age_days: int = 30) -> None:
        """Initialise the orphan scanner.

        Args:
            age_days: Fragments younger than this are excluded from
                orphan detection.
        """
        self.age_days = age_days

    def scan(self, vault_path: Path) -> OrphanResult:
        """Scan the vault for orphaned fragments.

        Every wiki-link in the vault is resolved to the page it actually
        names, then counted against that page alone. A fragment is orphaned
        when nothing links to it, it links to nothing else, and it is older
        than ``age_days``.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            An :class:`OrphanResult` with orphan paths and totals.
        """
        fragment_files = _list_fragment_files(vault_path)
        all_md_files = _list_all_md_files(vault_path)
        link_index = build_link_index(vault_path)

        incoming, outgoing = self._build_link_graph(all_md_files, link_index)

        now = datetime.now(tz=UTC)
        orphans = self._find_orphans(fragment_files, incoming, outgoing, now)

        return OrphanResult(
            orphan_paths=[str(p) for p in orphans],
            total_fragments=len(fragment_files),
        )

    @staticmethod
    def _build_link_graph(
        all_md_files: list[Path],
        link_index: LinkIndex,
    ) -> tuple[dict[Path, set[Path]], dict[Path, set[Path]]]:
        """Build page-keyed incoming and outgoing link maps for the vault.

        Keys and values are resolved page paths rather than filename stems,
        so a link written in alias form credits the page that declares the
        alias — and only that page.

        Args:
            all_md_files: All markdown files in the vault.
            link_index: Vault-wide name → page index.

        Returns:
            A ``(incoming, outgoing)`` pair: ``incoming`` maps a page to the
            files linking to it, ``outgoing`` maps a file to the pages it
            links to. Targets that resolve to nothing are dropped from both.
        """
        incoming: dict[Path, set[Path]] = {}
        outgoing: dict[Path, set[Path]] = {}
        for md_file in all_md_files:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            resolved = {
                page
                for target in _extract_wikilinks(content)
                if (page := link_index.resolve(target)) is not None
            }
            outgoing[md_file] = resolved
            for page in resolved:
                incoming.setdefault(page, set()).add(md_file)
        return incoming, outgoing

    def _find_orphans(
        self,
        fragment_files: list[Path],
        incoming: dict[Path, set[Path]],
        outgoing: dict[Path, set[Path]],
        now: datetime,
    ) -> list[Path]:
        """Filter fragments to those that are orphaned and old enough.

        Self-references are discounted in both directions: a note that only
        links to itself is as unconnected as one that links to nothing.

        Args:
            fragment_files: Fragment markdown files.
            incoming: Map of page to the files linking to it.
            outgoing: Map of file to the pages it links to.
            now: Current UTC datetime for age calculation.

        Returns:
            List of orphaned fragment paths.
        """
        orphans: list[Path] = []
        for frag_file in fragment_files:
            self_only = {frag_file}
            has_incoming = bool(incoming.get(frag_file, set()) - self_only)
            has_outgoing = bool(outgoing.get(frag_file, set()) - self_only)

            if has_incoming or has_outgoing:
                continue

            if self._is_old_enough(frag_file, now):
                orphans.append(frag_file)

        return orphans

    def _is_old_enough(self, frag_file: Path, now: datetime) -> bool:
        """Check if a fragment file is older than the age threshold.

        Reads the ``created`` field from frontmatter, falling back to
        file modification time.

        Args:
            frag_file: Path to the fragment markdown file.
            now: Current UTC datetime.

        Returns:
            True if the fragment is older than ``age_days``.
        """
        try:
            post = frontmatter.load(str(frag_file))
            created = post.get("created")
            created_dt = _parse_datetime(created)
            if created_dt is not None:
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=UTC)
                age = (now - created_dt).days
                return age >= self.age_days
        except Exception:
            logger.debug("Failed to read frontmatter from %s", frag_file)

        stat = frag_file.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        return (now - mtime).days >= self.age_days


class StaleReviewScanner:
    """Find review queue items older than a configurable threshold.

    Scans the vault for review queue markdown files and checks their
    generation timestamps.

    Attributes:
        age_days: Maximum age in days before a review file is stale
            (default 14).
    """

    def __init__(self, *, age_days: int = 14) -> None:
        """Initialise the stale review scanner.

        Args:
            age_days: Review files older than this are flagged as stale.
        """
        self.age_days = age_days

    def scan(self, vault_path: Path) -> StaleReviewResult:
        """Scan the vault for stale review queue files.

        Looks for files matching ``review-queue-*.md`` anywhere in the
        vault and checks their age based on the filename timestamp or
        file modification time.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            A :class:`StaleReviewResult` with stale paths and totals.
        """
        review_files = sorted(vault_path.rglob("review-queue-*.md"))
        now = datetime.now(tz=UTC)
        stale: list[str] = [
            str(review_file)
            for review_file in review_files
            if self._is_stale(review_file, now)
        ]

        return StaleReviewResult(
            stale_paths=stale,
            total_review_files=len(review_files),
        )

    def _is_stale(self, review_file: Path, now: datetime) -> bool:
        """Check whether a review file is older than the age threshold.

        Attempts to parse the timestamp from the filename
        (``review-queue-YYYY-MM-DD_HHMMSS.md``), falling back to file
        modification time.

        Args:
            review_file: Path to a review queue file.
            now: Current UTC datetime.

        Returns:
            True if the file is older than ``age_days``.
        """
        ts = self._parse_filename_timestamp(review_file)
        if ts is not None:
            return (now - ts).days >= self.age_days

        stat = review_file.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        return (now - mtime).days >= self.age_days

    def _parse_filename_timestamp(self, review_file: Path) -> datetime | None:
        """Extract a UTC datetime from a review queue filename.

        Expected format: ``review-queue-YYYY-MM-DD_HHMMSS.md``.

        Args:
            review_file: Path to the review file.

        Returns:
            Parsed datetime with UTC timezone, or None if parsing fails.
        """
        stem = review_file.stem
        prefix = "review-queue-"
        if not stem.startswith(prefix):
            return None
        ts_part = stem[len(prefix) :]
        try:
            return datetime.strptime(ts_part, "%Y-%m-%d_%H%M%S").replace(
                tzinfo=UTC,
            )
        except ValueError:
            return None


class BrokenLinkScanner:
    """Scan the vault for wiki-links and relative links to nonexistent files.

    The survey is :func:`creek.vault.links.iter_link_sources`: the whole vault
    minus Creek's own report folders (``00-Creek-Meta/Processing-Log/``,
    ``00-Creek-Meta/State/``, ``00-Creek-Meta/Ontology/``). Scanning
    ``01-Fragments`` alone — the behaviour before #1344 — made a dangling link
    on a thread, praxis or decision page invisible while ``creek lint``
    published the count as a whole-vault verdict. Scanning the *literal* whole
    vault is the other failure: the lint report renders every finding as
    ``- `src` → `[[target]]` ``, so three successive runs over a vault holding
    ONE genuine broken link reported 1, then 2, then 3.

    Wiki-links resolve through :func:`creek.vault.links.build_link_index`,
    which knows every name a page can be linked by — filename stem,
    frontmatter ``title``, and each ``aliases`` entry. Matching stems alone
    (the behaviour before #887) called 99.2% of the demo vault's links
    broken, because the linkers write date-prefixed filenames and put the
    human-readable name in ``aliases``.
    """

    def scan(self, vault_path: Path) -> BrokenLinkResult:
        """Scan the vault for broken links.

        Checks wiki-links (``[[Target]]``) and relative markdown links
        (``[text](path)``) in every surveyed source file — the whole vault
        except Creek's own report folders, which quote findings back and would
        otherwise inflate the count run over run.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            A :class:`BrokenLinkResult` whose ``total_files_scanned`` counts
            the files actually surveyed, so the "across N file(s)" line
            ``creek lint`` prints does not overstate its own coverage.
        """
        source_files = iter_link_sources(vault_path)
        link_index = build_link_index(vault_path)

        broken_links: dict[str, list[str]] = {}
        total_broken = 0

        for source_file in source_files:
            file_broken = self._check_file(source_file, link_index, vault_path)
            if file_broken:
                broken_links[str(source_file)] = file_broken
                total_broken += len(file_broken)

        return BrokenLinkResult(
            broken_links=broken_links,
            total_files_scanned=len(source_files),
            total_broken=total_broken,
        )

    def _check_file(
        self,
        source_file: Path,
        link_index: LinkIndex,
        vault_path: Path,
    ) -> list[str]:
        """Check a single file for broken links.

        Args:
            source_file: Path to the surveyed markdown file.
            link_index: Vault-wide name → page index. A wiki-link is broken
                only when nothing in the vault answers to its target under
                any of its names.
            vault_path: Root of the vault for resolving relative paths.

        Returns:
            List of broken link targets found in this file.
        """
        content = source_file.read_text(encoding="utf-8", errors="replace")
        broken: list[str] = [
            f"[[{target}]]"
            for target in _extract_wikilinks(content)
            if target not in link_index
        ]

        broken.extend(
            target
            for target in _extract_relative_links(content)
            if self._is_broken_local_link(target, source_file, vault_path)
        )

        return broken

    @staticmethod
    def _is_broken_local_link(
        target: str,
        source_file: Path,
        vault_path: Path,
    ) -> bool:
        """Check whether a relative target resolves to no existing file.

        Resolution is wrapped defensively: a pathological target (e.g. one
        with an over-long path segment) makes ``Path.exists()`` raise
        ``OSError`` (``ENAMETOOLONG``, ``ELOOP``, ...) rather than returning
        ``False``. Such targets cannot name a real local file, so they are
        treated as not-broken (skipped) instead of aborting the whole scan.

        Args:
            target: A relative link target (no URL scheme).
            source_file: The surveyed file containing the link.
            vault_path: Root of the vault for resolving relative paths.

        Returns:
            True if the target resolves to no existing file under either
            the source file's directory or the vault root.
        """
        try:
            resolved = (source_file.parent / target).resolve()
            vault_resolved = (vault_path / target).resolve()
            missing = not resolved.exists() and not vault_resolved.exists()
        except OSError:
            logger.debug("Skipping unresolvable link target %r", target)
            return False
        else:
            return missing


class DuplicateScanner:
    """Run normalized dedup sweep across fragment files.

    Uses the existing :class:`creek.clean.dedup.Deduplicator` to
    identify exact and normalized duplicate pairs.
    """

    def scan(self, vault_path: Path) -> DuplicateResult:
        """Scan fragments for duplicate content.

        Reads each fragment's content and registers it with the
        deduplicator. When a duplicate is found, both files are
        recorded as a candidate pair.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            A :class:`DuplicateResult` with candidate pairs and totals.
        """
        from creek.clean.dedup import Deduplicator

        fragment_files = _list_fragment_files(vault_path)
        dedup = Deduplicator()
        candidates: list[DuplicateCandidate] = []
        id_to_path: dict[str, str] = {}

        for frag_file in fragment_files:
            content, source, timestamp = self._read_fragment(frag_file)
            if content is None:
                continue

            result = dedup.register(source, timestamp, content)

            frag_id = result.matched_fragment_id
            if result.is_duplicate and frag_id is not None:
                original_path = id_to_path.get(frag_id, frag_id)
                candidates.append(
                    DuplicateCandidate(
                        file_a=original_path,
                        file_b=str(frag_file),
                        match_type=result.match_type,
                    ),
                )
            elif not result.is_duplicate:
                from creek.ingest.base import generate_fragment_id

                new_id = generate_fragment_id(source, timestamp, content)
                id_to_path[new_id] = str(frag_file)

        return DuplicateResult(
            candidates=candidates,
            total_fragments=len(fragment_files),
        )

    def _read_fragment(
        self,
        frag_file: Path,
    ) -> tuple[str | None, str, datetime]:
        """Read fragment content and metadata from a markdown file.

        Args:
            frag_file: Path to the fragment file.

        Returns:
            Tuple of (content, source, timestamp). Content is None if
            the file cannot be read or has no content.
        """
        try:
            post = frontmatter.load(str(frag_file))
        except Exception:
            logger.debug("Failed to read fragment %s", frag_file)
            return None, "", datetime.now(tz=UTC)

        content = post.content.strip() if post.content else ""
        if not content:
            return None, "", datetime.now(tz=UTC)

        source_meta = post.get("source", {})
        source = (
            str(source_meta.get("original_file", str(frag_file)))
            if isinstance(source_meta, dict)
            else str(frag_file)
        )
        created = post.get("created", datetime.now(tz=UTC))
        if not isinstance(created, datetime):
            created = datetime.now(tz=UTC)

        return content, source, created


class HygieneReporter:
    """Generate a comprehensive vault health summary report.

    Runs all scanners and aggregates results into a single
    :class:`HygieneReport`, optionally writing a markdown file.
    """

    def __init__(
        self,
        *,
        orphan_age_days: int = 30,
        stale_review_age_days: int = 14,
    ) -> None:
        """Initialise the reporter with scanner thresholds.

        Args:
            orphan_age_days: Age threshold for orphan detection.
            stale_review_age_days: Age threshold for stale reviews.
        """
        self.orphan_scanner = OrphanScanner(age_days=orphan_age_days)
        self.stale_review_scanner = StaleReviewScanner(age_days=stale_review_age_days)
        self.broken_link_scanner = BrokenLinkScanner()
        self.duplicate_scanner = DuplicateScanner()

    def generate(self, vault_path: Path) -> HygieneReport:
        """Run all scanners and compile a health report.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            A :class:`HygieneReport` with aggregated results.
        """
        orphans = self.orphan_scanner.scan(vault_path)
        stale = self.stale_review_scanner.scan(vault_path)
        broken = self.broken_link_scanner.scan(vault_path)
        dupes = self.duplicate_scanner.scan(vault_path)
        quality_dist = self._compute_quality_distribution(vault_path)

        return HygieneReport(
            total_fragments=orphans.total_fragments,
            orphan_count=len(orphans.orphan_paths),
            stale_review_count=len(stale.stale_paths),
            broken_link_count=broken.total_broken,
            duplicate_candidate_count=len(dupes.candidates),
            quality_distribution=quality_dist,
        )

    def write_markdown(self, report: HygieneReport, output_path: Path) -> Path:
        """Write a hygiene report as a markdown file.

        Args:
            report: The report to write.
            output_path: Path for the output file.

        Returns:
            The path to the written file.
        """
        lines = self._format_report(report)
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path

    def _compute_quality_distribution(
        self,
        vault_path: Path,
    ) -> dict[str, int]:
        """Compute quality score distribution across fragments.

        Args:
            vault_path: Root of the Obsidian vault.

        Returns:
            Mapping of quality action (accept/review/skip) to count.
        """
        from creek.clean.quality import QualityScorer

        scorer = QualityScorer()
        fragment_files = _list_fragment_files(vault_path)
        distribution: dict[str, int] = {"accept": 0, "review": 0, "skip": 0}

        for frag_file in fragment_files:
            content = self._read_content(frag_file)
            result = scorer.score(content)
            distribution[result.action] = distribution.get(result.action, 0) + 1

        return distribution

    def _read_content(self, frag_file: Path) -> str:
        """Read the body content from a frontmatter markdown file.

        Args:
            frag_file: Path to the file.

        Returns:
            The body content, or empty string on failure.
        """
        try:
            post = frontmatter.load(str(frag_file))
        except Exception:
            logger.debug("Failed to read content from %s", frag_file)
            return ""
        else:
            return post.content

    def _format_report(self, report: HygieneReport) -> list[str]:
        """Format a hygiene report as markdown lines.

        Args:
            report: The report to format.

        Returns:
            List of markdown lines.
        """
        lines = [
            "# Vault Hygiene Report",
            "",
            f"Generated: {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "",
            "## Summary",
            "",
            "| Metric | Count |",
            "|--------|-------|",
            f"| Total fragments | {report.total_fragments} |",
            f"| Orphaned fragments | {report.orphan_count} |",
            f"| Stale review files | {report.stale_review_count} |",
            f"| Broken links | {report.broken_link_count} |",
            f"| Duplicate candidates | {report.duplicate_candidate_count} |",
            "",
            "## Quality Distribution",
            "",
            "| Action | Count |",
            "|--------|-------|",
        ]

        for action, count in sorted(report.quality_distribution.items()):
            lines.append(f"| {action} | {count} |")

        lines.append("")
        return lines
