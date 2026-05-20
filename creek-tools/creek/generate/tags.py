"""Tag Garden generator for the Creek vault.

Maintains a tag taxonomy file at ``00-Creek-Meta/Tag-Garden.md`` that tracks
tag counts, identifies rapidly expanding tags, detects tag clusters via
co-occurrence analysis, and provides maintenance recommendations.

History is persisted to ``00-Creek-Meta/Processing-Log/tag-history.json``
for temporal growth tracking across scans.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import frontmatter

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class TagScanResult:
    """Result of scanning the vault for tags.

    Attributes:
        tag_counts: Mapping of tag name to occurrence count.
        tag_fragments: Mapping of tag name to list of fragment IDs.
    """

    tag_counts: dict[str, int]
    tag_fragments: dict[str, list[str]]


_SCAN_DIRS: list[str] = [
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "08-Decisions",
]
"""Vault subdirectories to scan for tagged markdown files."""

_GROWTH_THRESHOLD: float = 0.5
"""Minimum fractional growth (50%) to flag a tag as rapidly expanding."""

_CO_OCCURRENCE_MIN: int = 2
"""Minimum co-occurrence count for two tags to form a cluster."""

_BROAD_TAG_THRESHOLD: int = 100
"""Tags used more than this many times are suggested for splitting."""

_SIMILAR_DISTANCE_MAX: float = 0.8
"""Minimum SequenceMatcher ratio to consider two tags similar (merge candidate)."""


def _build_note(front: dict[str, str], body: str) -> str:
    """Build a markdown note with YAML frontmatter and body content.

    Args:
        front: Key-value pairs for the YAML frontmatter block.
        body: The markdown body content after the frontmatter.

    Returns:
        Complete markdown string with ``---`` delimited frontmatter.
    """
    lines = ["---"]
    for key, value in front.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


@dataclass
class _FragmentTags:
    """Tags extracted from a single vault file.

    Attributes:
        fragment_id: The ``id`` field from the file frontmatter.
        tags: List of tags found in the file frontmatter.
    """

    fragment_id: str
    tags: list[str] = field(default_factory=list)


class TagGardenGenerator:
    """Generate and maintain the Tag Garden for the Creek vault.

    Scans vault markdown files for tags, detects growth trends,
    finds co-occurrence clusters, and produces maintenance suggestions.

    Attributes:
        vault_path: Path to the root of the Obsidian vault.
        history_path: Path to the tag history JSON file.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialize the TagGardenGenerator with a vault path.

        Args:
            vault_path: Path to the root of the Obsidian vault directory.
        """
        self.vault_path = vault_path
        self.history_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        )

    def scan_tags(self) -> TagScanResult:
        """Scan vault frontmatter and count all tag occurrences.

        Reads markdown files from fragment, thread, eddy, praxis, and
        decision directories, extracting tags from YAML frontmatter.

        Returns:
            TagScanResult with tag counts and fragment-to-tag mappings.
        """
        tag_counts: dict[str, int] = defaultdict(int)
        tag_fragments: dict[str, list[str]] = defaultdict(list)

        for scan_dir in _SCAN_DIRS:
            dir_path = self.vault_path / scan_dir
            if not dir_path.is_dir():
                continue
            for md_file in dir_path.rglob("*.md"):
                ft = self._extract_tags(md_file)
                for tag in ft.tags:
                    tag_counts[tag] += 1
                    tag_fragments[tag].append(ft.fragment_id)

        return TagScanResult(
            tag_counts=dict(tag_counts),
            tag_fragments=dict(tag_fragments),
        )

    def detect_growth(
        self,
        current: TagScanResult,
        *,
        previous_counts: dict[str, int] | None,
    ) -> dict[str, object]:
        """Compare current scan against previous, flagging growth and new tags.

        Tags with >50% growth or that are entirely new are reported.

        Args:
            current: The current tag scan result.
            previous_counts: Tag counts from the previous scan, or None.

        Returns:
            Dict with ``new_tags`` (list of tag names) and
            ``growing_tags`` (dict mapping tag to growth details).
        """
        new_tags: list[str] = []
        growing_tags: dict[str, dict[str, int]] = {}

        if previous_counts is None:
            return {
                "new_tags": list(current.tag_counts.keys()),
                "growing_tags": {},
            }

        for tag, count in current.tag_counts.items():
            prev = previous_counts.get(tag)
            if prev is None:
                new_tags.append(tag)
            elif prev > 0 and (count - prev) / prev > _GROWTH_THRESHOLD:
                growing_tags[tag] = {"previous": prev, "current": count}

        return {"new_tags": new_tags, "growing_tags": growing_tags}

    def detect_clusters(self, scan: TagScanResult) -> list[dict[str, object]]:
        """Identify co-occurring tags via a co-occurrence matrix.

        Builds a matrix of how often each pair of tags appears on the
        same fragment, then returns pairs exceeding the minimum threshold.

        Args:
            scan: The current tag scan result.

        Returns:
            List of cluster dicts with ``tags`` (pair) and ``count``.
        """
        if not scan.tag_fragments:
            return []

        # Build reverse index: fragment_id -> set of tags
        frag_to_tags: dict[str, set[str]] = defaultdict(set)
        for tag, frag_ids in scan.tag_fragments.items():
            for frag_id in frag_ids:
                frag_to_tags[frag_id].add(tag)

        # Count co-occurrences
        co_occur: dict[tuple[str, str], int] = defaultdict(int)
        for tags_set in frag_to_tags.values():
            sorted_tags = sorted(tags_set)
            for i, tag_a in enumerate(sorted_tags):
                for tag_b in sorted_tags[i + 1 :]:
                    co_occur[(tag_a, tag_b)] += 1

        clusters: list[dict[str, object]] = []
        for (tag_a, tag_b), count in sorted(
            co_occur.items(),
            key=lambda x: x[1],
            reverse=True,
        ):
            if count >= _CO_OCCURRENCE_MIN:
                clusters.append(
                    {"tags": [tag_a, tag_b], "count": count},
                )

        return clusters

    def suggest_maintenance(
        self,
        scan: TagScanResult,
    ) -> list[dict[str, object]]:
        """Generate maintenance recommendations for the tag taxonomy.

        Recommends merging similar tags (edit distance < 3 chars difference),
        splitting broad tags (>100 uses), and flags single-use orphan tags.

        Args:
            scan: The current tag scan result.

        Returns:
            List of suggestion dicts with ``type`` and relevant details.
        """
        suggestions: list[dict[str, object]] = []
        tag_names = sorted(scan.tag_counts.keys())

        # Detect similar tags (merge candidates)
        for i, tag_a in enumerate(tag_names):
            for tag_b in tag_names[i + 1 :]:
                ratio = SequenceMatcher(None, tag_a, tag_b).ratio()
                if ratio >= _SIMILAR_DISTANCE_MAX and tag_a != tag_b:
                    suggestions.append(
                        {
                            "type": "merge",
                            "tag_a": tag_a,
                            "tag_b": tag_b,
                            "similarity": round(ratio, 2),
                        }
                    )

        # Detect broad tags (split candidates)
        for tag, count in scan.tag_counts.items():
            if count > _BROAD_TAG_THRESHOLD:
                suggestions.append({"type": "split", "tag": tag, "count": count})

        # Detect orphan tags (single use)
        for tag, count in scan.tag_counts.items():
            if count == 1:
                suggestions.append({"type": "orphan", "tag": tag})

        return suggestions

    def generate_garden(self) -> Path:
        """Generate the Tag Garden markdown file with all sections.

        Orchestrates scanning, growth detection, cluster detection,
        and suggestion generation, then writes the output to
        ``00-Creek-Meta/Tag-Garden.md`` and persists history.

        Returns:
            Path to the generated Tag-Garden.md file.
        """
        scan = self.scan_tags()
        previous_counts = self._load_previous_counts()
        growth = self.detect_growth(scan, previous_counts=previous_counts)
        clusters = self.detect_clusters(scan)
        suggestions = self.suggest_maintenance(scan)

        body = self._render_all_tags(scan)
        body += self._render_growing_tags(growth)
        body += self._render_clusters(clusters)
        body += self._render_suggestions(suggestions)

        front = {
            "type": "tag-garden",
            "title": '"Tag Garden"',
            "generated": datetime.now(tz=UTC).isoformat(),
        }

        output_path = self.vault_path / "00-Creek-Meta" / "Tag-Garden.md"
        output_path.write_text(_build_note(front, body), encoding="utf-8")

        self._save_history(scan)

        return output_path

    # ---- Private helpers ----

    @staticmethod
    def _extract_tags(md_file: Path) -> _FragmentTags:
        """Extract tags and ID from a markdown file's frontmatter.

        Args:
            md_file: Path to the markdown file.

        Returns:
            _FragmentTags with the file's ID and tag list.
        """
        post = frontmatter.load(str(md_file))
        raw_id = post.get("id", md_file.stem)
        frag_id: str = raw_id if isinstance(raw_id, str) else md_file.stem
        tags_raw = post.get("tags", [])
        tags: list[str] = list(tags_raw) if isinstance(tags_raw, list) else []
        return _FragmentTags(fragment_id=frag_id, tags=tags)

    def _load_previous_counts(self) -> dict[str, int] | None:
        """Load the most recent tag counts from history.

        Returns:
            Previous tag counts dict, or None if no history exists.
        """
        if not self.history_path.exists():
            return None
        history = json.loads(self.history_path.read_text(encoding="utf-8"))
        if not history:
            return None
        latest: dict[str, int] = history[-1]["tag_counts"]
        return latest

    def _save_history(self, scan: TagScanResult) -> None:
        """Append current scan to the tag history file.

        Args:
            scan: The current tag scan result to persist.
        """
        history: list[dict[str, object]] = []
        if self.history_path.exists():
            history = json.loads(
                self.history_path.read_text(encoding="utf-8"),
            )

        entry: dict[str, object] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "tag_counts": scan.tag_counts,
        }
        history.append(entry)

        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(
            json.dumps(history, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _render_all_tags(scan: TagScanResult) -> str:
        """Render the All Tags section of the Tag Garden.

        Args:
            scan: The current tag scan result.

        Returns:
            Markdown string for the All Tags section.
        """
        lines = ["# Tag Garden\n", "## All Tags\n"]
        if not scan.tag_counts:
            lines.append("*No tags found in vault.*\n")
        else:
            sorted_tags = sorted(
                scan.tag_counts.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            lines.append("| Tag | Count | Fragments |")
            lines.append("|-----|-------|-----------|")
            for tag, count in sorted_tags:
                frag_links = ", ".join(
                    f"[[{fid}]]" for fid in scan.tag_fragments.get(tag, [])[:5]
                )
                if len(scan.tag_fragments.get(tag, [])) > 5:
                    frag_links += ", ..."
                lines.append(f"| {tag} | {count} | {frag_links} |")
            lines.append("")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_growing_tags(growth: dict[str, object]) -> str:
        """Render the Growing Tags section of the Tag Garden.

        Args:
            growth: Growth detection results.

        Returns:
            Markdown string for the Growing Tags section.
        """
        lines = ["## Growing Tags\n"]
        new_tags: list[str] = growth.get("new_tags", [])  # type: ignore[assignment]
        growing_tags: dict[str, dict[str, int]] = growth.get("growing_tags", {})  # type: ignore[assignment]

        if not new_tags and not growing_tags:
            lines.append("*No significant growth detected.*\n")
        else:
            if new_tags:
                lines.append("### New Tags\n")
                for tag in sorted(new_tags):
                    lines.append(f"- **{tag}**")
                lines.append("")

            if growing_tags:
                lines.append("### Rapidly Growing\n")
                lines.append("| Tag | Previous | Current | Growth |")
                lines.append("|-----|----------|---------|--------|")
                for tag, info in sorted(growing_tags.items()):
                    prev = info["previous"]
                    curr = info["current"]
                    pct = round((curr - prev) / prev * 100) if prev else 0
                    lines.append(f"| {tag} | {prev} | {curr} | +{pct}% |")
                lines.append("")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_clusters(clusters: list[dict[str, object]]) -> str:
        """Render the Tag Clusters section of the Tag Garden.

        Args:
            clusters: List of co-occurrence cluster dicts.

        Returns:
            Markdown string for the Tag Clusters section.
        """
        lines = ["## Tag Clusters\n"]
        if not clusters:
            lines.append("*No significant tag clusters detected.*\n")
        else:
            lines.append("| Tag A | Tag B | Co-occurrences |")
            lines.append("|-------|-------|----------------|")
            for cluster in clusters:
                tags: list[str] = cluster["tags"]  # type: ignore[assignment]
                count = cluster["count"]
                lines.append(f"| {tags[0]} | {tags[1]} | {count} |")
            lines.append("")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_suggestions(
        suggestions: list[dict[str, object]],
    ) -> str:
        """Render the Maintenance Suggestions section of the Tag Garden.

        Args:
            suggestions: List of suggestion dicts.

        Returns:
            Markdown string for the Maintenance Suggestions section.
        """
        lines = ["## Maintenance Suggestions\n"]
        if not suggestions:
            lines.append("*No maintenance actions recommended.*\n")
        else:
            merges = [s for s in suggestions if s["type"] == "merge"]
            splits = [s for s in suggestions if s["type"] == "split"]
            orphans = [s for s in suggestions if s["type"] == "orphan"]
            lines.extend(_render_merge_candidates(merges))
            lines.extend(_render_split_candidates(splits))
            lines.extend(_render_orphan_tags(orphans))

        return "\n".join(lines) + "\n"


def _render_merge_candidates(merges: list[dict[str, object]]) -> list[str]:
    """Render merge candidate suggestions as markdown lines.

    Args:
        merges: List of merge suggestion dicts.

    Returns:
        Markdown lines for the merge candidates subsection.
    """
    if not merges:
        return []
    lines = ["### Merge Candidates\n"]
    for s in merges:
        lines.append(
            f"- Consider merging **{s['tag_a']}** and "
            f"**{s['tag_b']}** (similarity: {s['similarity']})",
        )
    lines.append("")
    return lines


def _render_split_candidates(splits: list[dict[str, object]]) -> list[str]:
    """Render split candidate suggestions as markdown lines.

    Args:
        splits: List of split suggestion dicts.

    Returns:
        Markdown lines for the split candidates subsection.
    """
    if not splits:
        return []
    lines = ["### Split Candidates\n"]
    for s in splits:
        lines.append(
            f"- **{s['tag']}** has {s['count']} uses — consider splitting",
        )
    lines.append("")
    return lines


def _render_orphan_tags(orphans: list[dict[str, object]]) -> list[str]:
    """Render orphan tag suggestions as markdown lines.

    Args:
        orphans: List of orphan suggestion dicts.

    Returns:
        Markdown lines for the orphan tags subsection.
    """
    if not orphans:
        return []
    lines = ["### Orphan Tags\n"]
    for s in orphans:
        lines.append(f"- **{s['tag']}** (single use)")
    lines.append("")
    return lines
