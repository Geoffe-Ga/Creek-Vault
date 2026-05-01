"""Unnamed weekly digest generator.

Section 10.1 of the Creek ontology marks ``10-Liminal/Unnamed/`` as the
most important research site in the vault: fragments that resist all
classification. This module surfaces those fragments every week as a
single reflection note.

The :class:`UnnamedDigestGenerator`:

- Collects fragments created within a seven-day window under
  ``10-Liminal/Unnamed/`` (skipping the ``Digests/`` subfolder).
- Clusters them via cosine similarity over sentence-transformer
  embeddings, using a deliberately loose threshold (default ``0.7``)
  so that even faint patterns surface.
- Writes a markdown digest at
  ``10-Liminal/Unnamed/Digests/<iso-year>-W<week>.md`` containing a
  fragment list with excerpts, cluster sections when similarity emerges,
  an open-ended reflection prompt, and week-over-week growth stats.
- Persists a history record at
  ``00-Creek-Meta/Processing-Log/unnamed-history.json`` so subsequent
  runs can report growth deltas.

The digest prompt is intentionally non-prescriptive — it asks what the
clustered fragments share that the current ontology cannot yet express,
honouring the ontology's stance that the Unnamed is a generative edge,
not an error to be corrected.
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
from pydantic import ValidationError

from creek.models import Fragment

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from creek.link.embeddings import EmbeddingLinker

logger = logging.getLogger(__name__)

_UNNAMED_SUBPATH = ("10-Liminal", "Unnamed")
_DIGESTS_SUBFOLDER = "Digests"
_HISTORY_SUBPATH = ("00-Creek-Meta", "Processing-Log", "unnamed-history.json")
_DEFAULT_SIMILARITY_THRESHOLD = 0.7
_EXCERPT_MAX_CHARS = 200
_WEEK_LENGTH_DAYS = 7
_REFLECTION_PROMPT = (
    "What do these have in common that the current ontology can't express?"
)


@dataclass(frozen=True)
class _HistoryEntry:
    """Single history record persisted per digest run.

    Attributes:
        week_start: ISO week-start date string.
        fragment_count: Number of unnamed fragments observed that week.
        total_unnamed: Total unnamed fragments in the vault at run time.
        cluster_count: Number of similarity clusters detected that week.
    """

    week_start: str
    fragment_count: int
    total_unnamed: int
    cluster_count: int

    def to_dict(self) -> dict[str, object]:
        """Return the entry as a JSON-serialisable dict.

        Returns:
            Dictionary with all history entry fields.
        """
        return {
            "week_start": self.week_start,
            "fragment_count": self.fragment_count,
            "total_unnamed": self.total_unnamed,
            "cluster_count": self.cluster_count,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }


class UnnamedDigestGenerator:
    """Generate weekly digest notes for the ``10-Liminal/Unnamed/`` folder.

    Each run collects the fragments created within the target week,
    clusters them via embedding similarity, and writes a markdown
    reflection note plus a history record.

    Attributes:
        embedding_linker: Linker used to embed fragments for clustering.
        similarity_threshold: Cosine similarity cutoff for grouping
            fragments into a cluster. Defaults to ``0.7`` per the
            ontology's "loose threshold" guidance.
    """

    def __init__(
        self,
        embedding_linker: EmbeddingLinker,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        """Initialise the generator.

        Args:
            embedding_linker: Linker used to produce embeddings.
            similarity_threshold: Cosine-similarity cutoff for cluster
                membership. Lower values surface looser patterns.
        """
        self.embedding_linker = embedding_linker
        self.similarity_threshold = similarity_threshold

    # ---- Public API ----

    def generate_weekly_digest(self, vault_path: Path, week_start: date) -> Path:
        """Generate the weekly Unnamed digest note.

        Orchestrates fragment collection, clustering, digest rendering,
        and history persistence.

        Args:
            vault_path: Path to the root of the Obsidian vault.
            week_start: Monday of the target ISO week (inclusive). The
                window is ``[week_start, week_start + 7 days)`` in
                date terms, which equals ``week_start`` through
                ``week_start + 6`` days inclusive — the convention also
                reflected in the rendered ``week_end`` frontmatter.

        Returns:
            Path to the generated digest markdown file.
        """
        all_unnamed = self.load_unnamed_fragments(vault_path)
        this_week = self.filter_fragments_in_week(all_unnamed, week_start)
        clusters = self.detect_unnamed_clusters(this_week)
        previous = self._load_history(vault_path)
        body = self._render_body(
            vault_path,
            this_week,
            clusters,
            week_start,
            previous=previous,
        )
        digest_path = self._write_digest(vault_path, week_start, body)
        self._append_history(
            vault_path,
            _HistoryEntry(
                week_start=week_start.isoformat(),
                fragment_count=len(this_week),
                total_unnamed=len(all_unnamed),
                cluster_count=len(clusters),
            ),
        )
        logger.info(
            "Generated unnamed digest for week %s at %s",
            week_start.isoformat(),
            digest_path,
        )
        return digest_path

    def load_unnamed_fragments(self, vault_path: Path) -> list[Fragment]:
        """Load every Fragment stored under ``10-Liminal/Unnamed/``.

        Skips the ``Digests/`` subdirectory and any markdown files that
        cannot be parsed as a :class:`Fragment`.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            List of Fragment objects parsed from the vault.
        """
        unnamed_dir = vault_path.joinpath(*_UNNAMED_SUBPATH)
        if not unnamed_dir.is_dir():
            return []

        fragments: list[Fragment] = []
        for md_file in sorted(unnamed_dir.rglob("*.md")):
            if _DIGESTS_SUBFOLDER in md_file.relative_to(unnamed_dir).parts:
                continue
            fragment = _load_fragment(md_file)
            if fragment is not None:
                fragments.append(fragment)
        return fragments

    @staticmethod
    def filter_fragments_in_week(
        fragments: list[Fragment],
        week_start: date,
    ) -> list[Fragment]:
        """Return the subset of *fragments* created within the target week.

        The window is ``[week_start, week_start + 7)`` days, matching the
        standard ISO-week convention (Monday inclusive, next Monday
        exclusive).

        Args:
            fragments: Candidate fragments.
            week_start: Monday of the target ISO week.

        Returns:
            Fragments whose ``created`` date falls in the window.
        """
        week_end = week_start + timedelta(days=_WEEK_LENGTH_DAYS)
        return [f for f in fragments if week_start <= f.created.date() < week_end]

    def detect_unnamed_clusters(
        self,
        fragments: list[Fragment],
    ) -> list[list[Fragment]]:
        """Cluster fragments via cosine similarity over their embeddings.

        Uses a union-find over pairs whose cosine similarity meets
        :attr:`similarity_threshold`. Clusters of size 1 are filtered
        out (a cluster implies at least one shared neighbour).

        Args:
            fragments: Fragments to cluster. Must be a list; empty
                input returns an empty list.

        Returns:
            Clusters as a list of fragment lists. Each cluster
            preserves input order. Outer list is sorted by descending
            cluster size, then by the first member's ID for stability.
        """
        if len(fragments) < 2:
            return []

        embeddings = self.embedding_linker.generate_embeddings(fragments)
        resonant_pairs = self._resonant_pairs(embeddings)
        components = _connected_components(len(fragments), resonant_pairs)
        clusters = [
            [fragments[idx] for idx in sorted(component)]
            for component in components
            if len(component) > 1
        ]
        clusters.sort(key=lambda c: (-len(c), c[0].id))
        return clusters

    # ---- Private helpers ----

    def _resonant_pairs(
        self,
        embeddings: dict[str, list[float]],
    ) -> list[tuple[int, int]]:
        """Return index pairs whose embeddings meet the similarity threshold.

        Computes cosine similarity inline rather than delegating to
        :meth:`EmbeddingLinker.find_resonances` to avoid mutating the
        linker's shared configuration.

        Args:
            embeddings: Mapping of fragment ID to embedding vector, in
                insertion order matching the original fragment list.

        Returns:
            List of ``(i, j)`` index pairs with ``i < j``.
        """
        ids = list(embeddings)
        if len(ids) < 2:
            return []
        import numpy as np  # lazy: numpy lives in the [embeddings] extra

        vectors = np.array([embeddings[fid] for fid in ids], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-10)
        normalized = vectors / norms
        similarity_matrix = normalized @ normalized.T
        pairs: list[tuple[int, int]] = []
        for i, j in itertools.combinations(range(len(ids)), 2):
            if float(similarity_matrix[i, j]) >= self.similarity_threshold:
                pairs.append((i, j))
        return pairs

    def _render_body(
        self,
        vault_path: Path,
        fragments: list[Fragment],
        clusters: list[list[Fragment]],
        week_start: date,
        *,
        previous: _HistoryEntry | None,
    ) -> str:
        """Render the complete markdown body for the digest.

        Args:
            vault_path: Path to the root of the Obsidian vault.
            fragments: Fragments observed this week.
            clusters: Similarity clusters detected this week.
            week_start: Monday of the target ISO week.
            previous: Previous history entry, or ``None`` when no prior
                run has been recorded.

        Returns:
            The markdown body (without frontmatter).
        """
        week_end = week_start + timedelta(days=_WEEK_LENGTH_DAYS - 1)
        excerpts = _build_excerpt_map(vault_path, fragments)
        sections = [
            f"# Unnamed Digest — {week_start.isoformat()} to {week_end.isoformat()}\n",
            _render_fragments_section(fragments, excerpts),
            _render_clusters_section(clusters),
            _render_prompt_section(),
            _render_growth_section(len(fragments), previous),
        ]
        return "\n".join(sections)

    def _write_digest(
        self,
        vault_path: Path,
        week_start: date,
        body: str,
    ) -> Path:
        """Serialise the digest note with frontmatter and write it to disk.

        Uses :func:`frontmatter.dumps` for consistency with
        :class:`creek.vault.writer.VaultWriter`.

        Args:
            vault_path: Path to the root of the Obsidian vault.
            week_start: Monday of the target ISO week.
            body: Rendered markdown body.

        Returns:
            Path to the written digest file.
        """
        digests_dir = vault_path.joinpath(*_UNNAMED_SUBPATH, _DIGESTS_SUBFOLDER)
        digests_dir.mkdir(parents=True, exist_ok=True)
        iso_year, iso_week, _ = week_start.isocalendar()
        filename = f"{iso_year}-W{iso_week:02d}.md"
        week_end = week_start + timedelta(days=_WEEK_LENGTH_DAYS - 1)
        post = frontmatter.Post(
            content=body,
            type="unnamed-digest",
            week_start=week_start.isoformat(),
            week_end=week_end.isoformat(),
            generated=datetime.now(tz=UTC).isoformat(),
        )
        digest_path = digests_dir / filename
        digest_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return digest_path

    @staticmethod
    def _history_path(vault_path: Path) -> Path:
        """Return the path to the unnamed-history JSON file.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the JSON history file.
        """
        return vault_path.joinpath(*_HISTORY_SUBPATH)

    def _load_history(self, vault_path: Path) -> _HistoryEntry | None:
        """Load the most recent history entry, if any.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            The last recorded :class:`_HistoryEntry`, or ``None`` when
            the history file is missing or empty.
        """
        path = self._history_path(vault_path)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Corrupt unnamed history at %s; treating as no prior data",
                path,
            )
            return None
        if not entries:
            return None
        last = entries[-1]
        return _HistoryEntry(
            week_start=str(last.get("week_start", "")),
            fragment_count=int(last.get("fragment_count", 0)),
            total_unnamed=int(last.get("total_unnamed", 0)),
            cluster_count=int(last.get("cluster_count", 0)),
        )

    def _append_history(
        self,
        vault_path: Path,
        entry: _HistoryEntry,
    ) -> None:
        """Append *entry* to the history log.

        Args:
            vault_path: Path to the root of the Obsidian vault.
            entry: History record to persist.
        """
        path = self._history_path(vault_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = []
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                try:
                    entries = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "Corrupt unnamed history at %s; overwriting with fresh log",
                        path,
                    )
                    entries = []
        entries.append(entry.to_dict())
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


# ---- Module-level helpers ----


def _load_fragment(md_file: Path) -> Fragment | None:
    """Parse a markdown file into a :class:`Fragment`, or ``None`` on failure.

    Args:
        md_file: Path to the markdown file.

    Returns:
        The parsed Fragment, or ``None`` when the file is not a valid
        fragment record.
    """
    try:
        post = frontmatter.load(str(md_file))
    except (OSError, ValueError):
        logger.debug("Skipping unreadable markdown file: %s", md_file)
        return None
    metadata = dict(post.metadata)
    if metadata.get("type") != "fragment":
        return None
    try:
        return Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None


def _connected_components(
    node_count: int,
    edges: list[tuple[int, int]],
) -> list[list[int]]:
    """Compute connected components via union-find.

    Args:
        node_count: Total number of nodes.
        edges: Undirected edges as ``(u, v)`` pairs.

    Returns:
        Components as lists of node indices, ordered by ascending root.
    """
    parent = list(range(node_count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for u, v in edges:
        root_u = find(u)
        root_v = find(v)
        if root_u != root_v:
            parent[root_u] = root_v

    buckets: dict[int, list[int]] = {}
    for node in range(node_count):
        buckets.setdefault(find(node), []).append(node)
    return [buckets[root] for root in sorted(buckets)]


def _excerpt_from_file(fragment_path: Path | None) -> str:
    """Read a short excerpt from the fragment's markdown body.

    Args:
        fragment_path: Path to the fragment file, or ``None`` when
            unavailable.

    Returns:
        A trimmed excerpt of at most :data:`_EXCERPT_MAX_CHARS`
        characters; empty string when no body is present.
    """
    if fragment_path is None or not fragment_path.exists():
        return ""
    post = frontmatter.load(str(fragment_path))
    body = (post.content or "").strip().replace("\n", " ")
    if not body:
        return ""
    if len(body) <= _EXCERPT_MAX_CHARS:
        return body
    return body[: _EXCERPT_MAX_CHARS - 1].rstrip() + "…"


def _find_fragment_file(vault_path: Path, fragment_id: str) -> Path | None:
    """Locate the markdown file for a fragment by ID, under 10-Liminal/Unnamed/.

    Args:
        vault_path: Path to the root of the Obsidian vault.
        fragment_id: Fragment ID to find.

    Returns:
        The matching markdown path, or ``None`` if not found.
    """
    unnamed_dir = vault_path.joinpath(*_UNNAMED_SUBPATH)
    if not unnamed_dir.is_dir():
        return None
    for md_file in unnamed_dir.rglob("*.md"):
        if _DIGESTS_SUBFOLDER in md_file.relative_to(unnamed_dir).parts:
            continue
        post = frontmatter.load(str(md_file))
        if post.get("id") == fragment_id:
            return md_file
    return None


def _render_fragments_section(
    fragments: list[Fragment],
    excerpts: dict[str, str],
) -> str:
    """Render the per-fragment listing section.

    Args:
        fragments: Fragments observed this week.
        excerpts: Mapping of fragment ID to a short body excerpt.

    Returns:
        Markdown string for the Fragments section.
    """
    lines = ["## Fragments This Week\n"]
    if not fragments:
        lines.append("No unnamed fragments recorded this week.\n")
        return "\n".join(lines) + "\n"
    for frag in fragments:
        created = frag.created.date().isoformat()
        lines.append(f"### {frag.title}\n")
        lines.append(f"- Link: [[{frag.id}]]")
        lines.append(f"- Created: {created}")
        excerpt = excerpts.get(frag.id, "")
        if excerpt:
            lines.append(f"- Excerpt: {excerpt}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _build_excerpt_map(
    vault_path: Path,
    fragments: list[Fragment],
) -> dict[str, str]:
    """Build a mapping of fragment ID to a short markdown body excerpt.

    Args:
        vault_path: Path to the root of the Obsidian vault.
        fragments: Fragments whose excerpts to resolve.

    Returns:
        Mapping of fragment ID to excerpt string.
    """
    excerpts: dict[str, str] = {}
    for frag in fragments:
        path = _find_fragment_file(vault_path, frag.id)
        excerpts[frag.id] = _excerpt_from_file(path)
    return excerpts


def _render_clusters_section(clusters: list[list[Fragment]]) -> str:
    """Render the Similarity Clusters section.

    Args:
        clusters: Detected clusters.

    Returns:
        Markdown string for the Similarity Clusters section.
    """
    lines = ["## Similarity Clusters\n"]
    if not clusters:
        lines.append("No similarity clusters emerged this week.\n")
        return "\n".join(lines) + "\n"
    for index, cluster in enumerate(clusters, start=1):
        lines.append(f"### Cluster {index}\n")
        for frag in cluster:
            lines.append(f"- [[{frag.id}]] — {frag.title}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_prompt_section() -> str:
    """Render the open-ended reflection prompt section.

    Returns:
        Markdown string for the Reflection Prompt section.
    """
    return (
        "## Reflection Prompt\n\n"
        f"> {_REFLECTION_PROMPT}\n\n"
        "What is the Unnamed asking of the ontology?\n\n"
    )


def _render_growth_section(
    this_week_count: int,
    previous: _HistoryEntry | None,
) -> str:
    """Render the Week-over-Week Growth section.

    Args:
        this_week_count: Number of unnamed fragments this week.
        previous: Previous history entry, or ``None`` when this is the
            first run.

    Returns:
        Markdown string for the growth section.
    """
    lines = ["## Week-over-Week Growth\n"]
    lines.append(f"- This week: {this_week_count} unnamed fragment(s)")
    if previous is None:
        lines.append("- Previous week: (no prior digest recorded)")
        lines.append("")
        return "\n".join(lines) + "\n"
    delta = this_week_count - previous.fragment_count
    sign = "+" if delta >= 0 else ""
    lines.append(
        f"- Previous week ({previous.week_start}): {previous.fragment_count}"
        f" unnamed fragment(s)",
    )
    lines.append(f"- Change: {sign}{delta}")
    lines.append("")
    return "\n".join(lines) + "\n"
