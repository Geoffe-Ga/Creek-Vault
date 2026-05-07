"""Compost tracking — abandoned threads and projects as composted notes.

Implements Section 10.4 of the Creek Ontology. The :class:`CompostTracker`
surfaces threads, fragments, and projects that have fallen dormant or been
explicitly abandoned, writes ``10-Liminal/Compost/`` notes preserving what
each idea was, why it faded, and what energy may still be alive, and
generates an overview report cross-referencing active threads.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter

from creek.models import (
    Confidence,
    Fragment,
    PraxisPotential,
    Thread,
    ThreadStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

ABANDONMENT_KEYWORDS: tuple[str, ...] = (
    "gave up on",
    "abandoned",
    "never finished",
    "put aside",
    "shelved",
)
"""Case-insensitive phrases flagging a fragment as describing abandonment."""

_DORMANCY_DAYS: int = 180
"""Threads without new fragments for this many days become compost."""

_PROJECT_GAP_DAYS: int = 180
"""Projects whose latest fragment is older than this are treated as gone."""

_PROJECT_MIN_FRAGMENTS: int = 2
"""A project must appear in at least this many fragments to be tracked."""

_HIGH_CONFIDENCE: frozenset[str] = frozenset(
    {Confidence.SETTLED.value, Confidence.CONVICTION.value},
)
"""Confidence levels whose fragments count as surviving energy."""

_UNKNOWN_REASON: str = "unknown"
"""Rendered when a candidate has no recorded reason."""


@dataclass
class CompostCandidate:
    """A single compost candidate flagged by :class:`CompostTracker`.

    Attributes:
        source_type: One of ``"thread"``, ``"fragment"``, or ``"project"``.
        source_id: Thread ID, fragment ID, or project tag — the key that
            uniquely identifies the source in the vault.
        title: Human-readable title of the thread, fragment, or project.
        reason: Explanation of why the source was composted (empty string
            when unknown; the note body renders ``unknown`` in that case).
        fragment_ids: Every fragment ID linked to the source. For project
            candidates, this is the set of fragments that carried the tag.
        energy_fragment_ids: IDs of fragments whose confidence or
            ``praxis_potential`` marks them as crystallised thinking worth
            preserving ("what energy remains").
        matched_keywords: Abandonment keywords matched against the source.
            Empty for thread and project candidates.
        energy_excerpts: Optional short excerpts describing the surviving
            energy; rendered as bullet points under "What energy remains".
    """

    source_type: str
    source_id: str
    title: str
    reason: str
    fragment_ids: list[str] = field(default_factory=list)
    energy_fragment_ids: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    energy_excerpts: list[str] = field(default_factory=list)


# ---- Helpers ----


def _confidence_value(fragment: Fragment) -> str:
    """Return the fragment's voice confidence as a string (or empty)."""
    return str(fragment.voice.confidence) if fragment.voice.confidence else ""


def _fragment_is_energetic(fragment: Fragment) -> bool:
    """Return whether a fragment carries crystallised, surviving energy."""
    if _confidence_value(fragment) in _HIGH_CONFIDENCE:
        return True
    return str(fragment.praxis_potential) == PraxisPotential.EXPLICIT.value


def _fragment_thread_titles(fragment: Fragment) -> list[str]:
    """Extract wiki-linked thread titles from a fragment's ``threads`` list."""
    titles: list[str] = []
    for raw in fragment.threads:
        match = re.fullmatch(r"\[\[(.+)\]\]", raw.strip())
        titles.append(match.group(1) if match else raw.strip())
    return titles


def _matched_abandonment_keywords(title: str) -> list[str]:
    """Return abandonment keywords present in *title* (case-insensitive)."""
    lowered = title.lower()
    return [kw for kw in ABANDONMENT_KEYWORDS if kw in lowered]


def _sanitize_filename(title: str) -> str:
    """Sanitise a title for use as part of a filename."""
    cleaned = re.sub(r"[^\w\s-]", "", title).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:80] or "compost"


class CompostTracker:
    """Track, record, and report on composted threads and projects.

    Attributes:
        dormancy_days: Idle window after which threads become compost.
        project_gap_days: Gap after which a tagged project is compost.
        project_min_fragments: Minimum fragment count to track a project.
    """

    def __init__(
        self,
        *,
        dormancy_days: int = _DORMANCY_DAYS,
        project_gap_days: int = _PROJECT_GAP_DAYS,
        project_min_fragments: int = _PROJECT_MIN_FRAGMENTS,
        now: datetime | None = None,
    ) -> None:
        """Initialise the tracker.

        Args:
            dormancy_days: Threads without new fragments for this many
                days become compost candidates. Defaults to 180.
            project_gap_days: Projects whose most recent fragment is
                older than this (in days) count as compost. Defaults to
                180.
            project_min_fragments: Minimum fragment count for a project
                (tag) to be tracked. Defaults to 2.
            now: Reference "now" for age calculations. Defaults to
                :func:`datetime.now` (timezone-naive). Useful for
                deterministic tests.
        """
        self.dormancy_days = dormancy_days
        self.project_gap_days = project_gap_days
        self.project_min_fragments = project_min_fragments
        self._now = now or datetime.now(tz=UTC).replace(tzinfo=None)

    # ---- Detection ----

    def detect_compost_candidates(
        self,
        threads: list[Thread],
        fragments: list[Fragment],
    ) -> list[CompostCandidate]:
        """Scan *threads* and *fragments* for compost candidates.

        The detection covers three sources:

        1. Threads with ``status == resolved`` or whose ``last_seen`` is
           older than :attr:`dormancy_days`.
        2. Fragments whose title contains an abandonment keyword.
        3. Tagged projects that appear in early fragments but have not
           been mentioned for more than :attr:`project_gap_days`.

        Args:
            threads: Threads to scan.
            fragments: Fragments to scan (used for keyword detection,
                project tracking, and energy extraction).

        Returns:
            A list of :class:`CompostCandidate` models, ordered
            thread-candidates → fragment-candidates → project-candidates.
        """
        thread_candidates = self._detect_threads(threads, fragments)
        fragment_candidates = self._detect_keyword_fragments(fragments)
        project_candidates = self._detect_disappeared_projects(fragments)
        return [*thread_candidates, *fragment_candidates, *project_candidates]

    def _detect_threads(
        self,
        threads: list[Thread],
        fragments: list[Fragment],
    ) -> list[CompostCandidate]:
        """Detect compost candidates whose source is a thread."""
        today = self._now.date()
        candidates: list[CompostCandidate] = []
        for thread in threads:
            reason = self._thread_reason(thread, today)
            if reason is None:
                continue
            related = [
                frag
                for frag in fragments
                if thread.title in _fragment_thread_titles(frag)
            ]
            candidates.append(
                CompostCandidate(
                    source_type="thread",
                    source_id=thread.id,
                    title=thread.title,
                    reason=reason,
                    fragment_ids=[frag.id for frag in related],
                    energy_fragment_ids=[
                        frag.id for frag in related if _fragment_is_energetic(frag)
                    ],
                    energy_excerpts=[
                        frag.title for frag in related if _fragment_is_energetic(frag)
                    ],
                ),
            )
        return candidates

    def _thread_reason(self, thread: Thread, today: date) -> str | None:
        """Return a compost reason for *thread*, or ``None`` if not a candidate."""
        if str(thread.status) == ThreadStatus.RESOLVED.value:
            return "Thread marked resolved"
        days_since = (today - thread.last_seen).days
        if days_since > self.dormancy_days:
            return f"Dormant for {days_since} days"
        return None

    def _detect_keyword_fragments(
        self,
        fragments: list[Fragment],
    ) -> list[CompostCandidate]:
        """Detect fragments whose title signals abandonment."""
        candidates: list[CompostCandidate] = []
        for fragment in fragments:
            matched = _matched_abandonment_keywords(fragment.title)
            if not matched:
                continue
            candidates.append(
                CompostCandidate(
                    source_type="fragment",
                    source_id=fragment.id,
                    title=fragment.title,
                    reason=f"Abandonment keyword: {matched[0]}",
                    fragment_ids=[fragment.id],
                    energy_fragment_ids=(
                        [fragment.id] if _fragment_is_energetic(fragment) else []
                    ),
                    matched_keywords=matched,
                    energy_excerpts=(
                        [fragment.title] if _fragment_is_energetic(fragment) else []
                    ),
                ),
            )
        return candidates

    def _detect_disappeared_projects(
        self,
        fragments: list[Fragment],
    ) -> list[CompostCandidate]:
        """Detect tagged projects that have fallen silent."""
        cutoff = self._now - timedelta(days=self.project_gap_days)
        tag_fragments = self._group_fragments_by_tag(fragments)
        candidates: list[CompostCandidate] = []
        for tag, frags in tag_fragments.items():
            candidate = self._project_candidate(tag, frags, cutoff)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _group_fragments_by_tag(
        fragments: list[Fragment],
    ) -> dict[str, list[Fragment]]:
        """Group fragments by each tag they carry."""
        grouped: dict[str, list[Fragment]] = defaultdict(list)
        for fragment in fragments:
            for tag in fragment.tags:
                grouped[tag].append(fragment)
        return grouped

    def _project_candidate(
        self,
        tag: str,
        frags: list[Fragment],
        cutoff: datetime,
    ) -> CompostCandidate | None:
        """Return a project candidate for *tag*, or ``None`` if still active."""
        if len(frags) < self.project_min_fragments:
            return None
        last_seen = max(frag.created for frag in frags)
        if last_seen >= cutoff:
            return None
        days = (self._now - last_seen).days
        energetic = [frag for frag in frags if _fragment_is_energetic(frag)]
        return CompostCandidate(
            source_type="project",
            source_id=tag,
            title=tag,
            reason=f"Project silent for {days} days",
            fragment_ids=[frag.id for frag in frags],
            energy_fragment_ids=[frag.id for frag in energetic],
            energy_excerpts=[frag.title for frag in energetic],
        )

    # ---- Note generation ----

    def create_compost_note(
        self,
        candidate: CompostCandidate,
        vault_path: Path,
    ) -> Path:
        """Write a compost note for *candidate* into the vault.

        The note lives at ``10-Liminal/Compost/<date>-<sanitised-title>.md``.
        Frontmatter captures the source reference, composted date, reason,
        tags, and fragment links. The body carries the three required
        sections ("What it was", "Why it composted", "What energy
        remains") plus a trailing list of all related fragments.

        Args:
            candidate: The compost candidate to record.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the written note.
        """
        compost_dir = vault_path / "10-Liminal" / "Compost"
        compost_dir.mkdir(parents=True, exist_ok=True)

        today = self._now.date()
        metadata = self._build_metadata(candidate, today)
        body = self._render_body(candidate)
        post = frontmatter.Post(content=body, **metadata)

        filename = f"{today.isoformat()}-{_sanitize_filename(candidate.title)}.md"
        note_path = compost_dir / filename
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    @staticmethod
    def _build_metadata(
        candidate: CompostCandidate,
        today: date,
    ) -> dict[str, object]:
        """Build the frontmatter metadata dict for *candidate*."""
        metadata: dict[str, object] = {
            "type": "compost",
            "title": candidate.title,
            "composted_date": today.isoformat(),
            "reason": candidate.reason,
            "fragments": [f"[[{fid}]]" for fid in candidate.fragment_ids],
            "tags": ["compost"],
        }
        if candidate.source_type == "thread":
            metadata["original_thread"] = f"[[{candidate.source_id}]]"
        elif candidate.source_type == "fragment":
            metadata["original_fragment"] = f"[[{candidate.source_id}]]"
        else:
            metadata["original_project"] = candidate.source_id
        if candidate.matched_keywords:
            metadata["matched_keywords"] = list(candidate.matched_keywords)
        return metadata

    @staticmethod
    def _render_body(candidate: CompostCandidate) -> str:
        """Render the markdown body for *candidate*."""
        lines: list[str] = []
        lines.append("## What it was")
        lines.append("")
        lines.append(
            f"A {candidate.source_type} titled **{candidate.title}** that "
            "has since composted back into the vault.",
        )
        lines.append("")
        lines.append("## Why it composted")
        lines.append("")
        lines.append(candidate.reason or _UNKNOWN_REASON)
        lines.append("")
        lines.append("## What energy remains")
        lines.append("")
        if candidate.energy_fragment_ids:
            for fid in candidate.energy_fragment_ids:
                lines.append(f"- [[{fid}]]")
            if candidate.energy_excerpts:
                lines.append("")
                for excerpt in candidate.energy_excerpts:
                    lines.append(f"> {excerpt}")
        else:
            lines.append("_No crystallised insights were flagged for preservation._")
        lines.append("")
        lines.append("## Related Fragments")
        lines.append("")
        if candidate.fragment_ids:
            for fid in candidate.fragment_ids:
                lines.append(f"- [[{fid}]]")
        else:
            lines.append("_No related fragments were recorded._")
        lines.append("")
        return "\n".join(lines)

    # ---- Reporting ----

    def generate_compost_report(self, vault_path: Path) -> Path:
        """Generate the vault-level compost overview report.

        The report lives at ``10-Liminal/Compost/_Compost-Report.md`` and
        contains a Dataview query that lists every compost note plus a
        section cross-referencing currently active threads (so you can
        notice when a composted idea resurfaces in new growth).

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the generated report file.
        """
        compost_dir = vault_path / "10-Liminal" / "Compost"
        compost_dir.mkdir(parents=True, exist_ok=True)
        report_path = compost_dir / "_Compost-Report.md"

        existing_notes = self._load_existing_compost_notes(compost_dir, report_path)
        active_threads = self._load_active_threads(vault_path / "02-Threads")

        text = self._render_report(existing_notes, active_threads, self._now.date())
        report_path.write_text(text, encoding="utf-8")
        return report_path

    @staticmethod
    def _load_existing_compost_notes(
        compost_dir: Path,
        report_path: Path,
    ) -> list[tuple[str, str]]:
        """Return ``(title, filename)`` tuples for compost notes in *compost_dir*."""
        notes: list[tuple[str, str]] = []
        for md_file in sorted(compost_dir.glob("*.md")):
            if md_file == report_path:
                continue
            try:
                post = frontmatter.load(str(md_file))
            except (OSError, ValueError):
                continue
            if post.get("type") != "compost":
                continue
            title = str(post.get("title") or md_file.stem)
            notes.append((title, md_file.stem))
        return notes

    @staticmethod
    def _load_active_threads(threads_dir: Path) -> list[tuple[str, str]]:
        """Return ``(title, id)`` tuples for active thread notes."""
        if not threads_dir.exists():
            return []
        active: list[tuple[str, str]] = []
        for md_file in sorted(threads_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(md_file))
            except (OSError, ValueError):
                continue
            status = str(post.get("status") or "")
            if status != ThreadStatus.ACTIVE.value:
                continue
            title = str(post.get("title") or md_file.stem)
            thread_id = str(post.get("id") or md_file.stem)
            active.append((title, thread_id))
        return active

    @staticmethod
    def _render_report(
        compost_notes: list[tuple[str, str]],
        active_threads: list[tuple[str, str]],
        generated_on: date,
    ) -> str:
        """Render the full markdown report text."""
        lines = [
            "---",
            "title: Compost Report",
            "type: compost-report",
            f"generated: {generated_on.isoformat()}",
            "---",
            "",
            "# Compost Report",
            "",
            "This report surveys every composted note in the vault and ",
            "cross-references them against the threads currently alive.",
            "",
            "## All Compost Notes",
            "",
            "```dataview",
            "TABLE composted_date, reason",
            'FROM "10-Liminal/Compost"',
            'WHERE type = "compost"',
            "SORT composted_date DESC",
            "```",
            "",
            "## Composted Notes (snapshot)",
            "",
        ]
        if compost_notes:
            for title, stem in compost_notes:
                lines.append(f"- [[{stem}|{title}]]")
        else:
            lines.append("_No compost notes recorded yet._")
        lines.extend(["", "## Active Threads", ""])
        lines.append(
            "Use this list to notice when composted ideas feed new growth:",
        )
        lines.append("")
        if active_threads:
            for title, thread_id in active_threads:
                lines.append(f"- [[{thread_id}|{title}]]")
        else:
            lines.append("_No active threads recorded yet._")
        lines.append("")
        return "\n".join(lines)
