"""Compost tracking — abandoned threads and projects as composted notes.

Implements Section 10.4 of the Creek Ontology. The :class:`CompostTracker`
surfaces threads, fragments, and projects that have fallen dormant or been
explicitly abandoned, writes ``10-Liminal/Compost/`` notes preserving what
each idea was, why it faded, and what energy may still be alive, and
generates an overview report cross-referencing active threads.

FEAT-018 replaced the five-phrase ``ABANDONMENT_KEYWORDS`` regex with a
two-stage detection: an embedding-similarity gate against curated
exemplars (:mod:`creek.generate.compost_embedding`) followed by an
optional LLM verifier (:mod:`creek.generate.compost_verifier`) whose
``ambiguous`` verdicts route to a vault-relative review queue.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter

from creek.generate.compost_verifier import CompostVerdict
from creek.models import (
    Confidence,
    Fragment,
    PraxisPotential,
    PrivacyTier,
    Thread,
    ThreadStatus,
)
from creek.time import effective_authored_at, ensure_aware, now_la

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from creek.generate.compost_verifier import SupportsVerifyCompost

logger = logging.getLogger(__name__)

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
        energy_excerpts: Optional short excerpts describing the surviving
            energy; rendered as bullet points under "What energy remains".
        similarity: Embedding-gate cosine similarity to the closest
            exemplar. ``None`` for thread and project candidates and
            for fragment candidates produced before FEAT-018.
        verifier_reasoning: One-sentence reason returned by the LLM
            verifier (:mod:`creek.generate.compost_verifier`).
            ``None`` when the verifier was skipped or the candidate
            originated from a thread/project source.
        for_review: When ``True`` the candidate routes to the operator
            review queue (``CompostConfig.review_queue_relpath``)
            rather than the canonical compost folder. Set for
            ``ambiguous`` verifier verdicts.
    """

    source_type: str
    source_id: str
    title: str
    reason: str
    fragment_ids: list[str] = field(default_factory=list)
    energy_fragment_ids: list[str] = field(default_factory=list)
    energy_excerpts: list[str] = field(default_factory=list)
    similarity: float | None = None
    verifier_reasoning: str | None = None
    for_review: bool = False


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


def _is_paradox(fragment: Fragment) -> bool:
    """Return whether *fragment* is a paradox note (skipped by compost detection)."""
    return "paradox" in fragment.tags


def _is_intimate(fragment: Fragment) -> bool:
    """Return whether *fragment* carries the intimate privacy tier."""
    return fragment.privacy_tier == PrivacyTier.INTIMATE


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
        similarity_fn: Callable[[str], float] | None = None,
        verifier: SupportsVerifyCompost | None = None,
        embedding_threshold: float = 0.6,
        skip_paradox: bool = True,
        skip_intimate: bool = True,
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
                :func:`creek.time.now_la` — tz-aware, America/Los_Angeles
                — so the dates stamped on compost notes and the overview
                report follow the same calendar as the rest of the vault
                (issue #938; a UTC-derived clock ran a day ahead of LA
                for the last several hours of every LA day). A naive
                value passed here is still accepted and read as an LA
                wall-clock reading, keeping existing callers working.
                Useful for deterministic tests.
            similarity_fn: Closure mapping a text string to its maximum
                cosine similarity against the compost exemplar set
                (see :mod:`creek.generate.compost_embedding`). When
                ``None``, fragment-level compost detection is skipped
                — thread-level dormancy and project-silence detection
                still run.
            verifier: Optional LLM verifier. When supplied, every
                fragment that clears *embedding_threshold* is sent
                through ``verifier.verify(...)``; ``yes`` → canonical
                compost, ``ambiguous`` → review queue
                (``for_review=True``), ``no`` → skipped. When ``None``,
                clearing the embedding threshold alone accepts the
                fragment as canonical compost.
            embedding_threshold: Minimum cosine similarity (against the
                exemplars) required for a fragment to advance to the
                verifier. Defaults to 0.6 — deliberately wide-net.
            skip_paradox: When ``True``, fragments tagged ``paradox``
                are excluded from compost detection. Defaults to
                ``True``.
            skip_intimate: When ``True``, fragments with
                ``privacy_tier == intimate`` are excluded from compost
                detection. Defaults to ``True`` (matching the default
                policy in :mod:`creek.classify.privacy_filter`).
        """
        self.dormancy_days = dormancy_days
        self.project_gap_days = project_gap_days
        self.project_min_fragments = project_min_fragments
        self._now = ensure_aware(now) if now is not None else now_la()
        self._similarity_fn = similarity_fn
        self._verifier = verifier
        self._embedding_threshold = embedding_threshold
        self._skip_paradox = skip_paradox
        self._skip_intimate = skip_intimate

    # ---- Detection ----

    def detect_compost_candidates(
        self,
        threads: list[Thread],
        fragments: list[Fragment],
        *,
        fragment_bodies: Mapping[str, str] | None = None,
    ) -> list[CompostCandidate]:
        """Scan *threads* and *fragments* for compost candidates.

        The detection covers three sources:

        1. Threads with ``status == resolved`` or whose ``last_seen`` is
           older than :attr:`dormancy_days`.
        2. Fragments whose embedding similarity to the curated compost
           exemplar set exceeds :attr:`_embedding_threshold` (FEAT-018).
           Optionally verified by an LLM before acceptance.
        3. Tagged projects that appear in early fragments but have not
           been mentioned for more than :attr:`project_gap_days`.

        Paradox-tagged and intimate-tier fragments are skipped per
        ``skip_paradox`` / ``skip_intimate`` on the constructor.

        Args:
            threads: Threads to scan.
            fragments: Fragments to scan (used for embedding detection,
                project tracking, and energy extraction).
            fragment_bodies: Optional mapping of fragment ID to body
                text. When supplied, the embedding gate uses
                ``title + body``; when omitted, title-only.

        Returns:
            A list of :class:`CompostCandidate` models, ordered
            thread-candidates → fragment-candidates → project-candidates.
        """
        thread_candidates = self._detect_threads(threads, fragments)
        fragment_candidates = self._detect_abandonment_fragments(
            fragments,
            fragment_bodies or {},
        )
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

    def _detect_abandonment_fragments(
        self,
        fragments: list[Fragment],
        bodies: Mapping[str, str],
    ) -> list[CompostCandidate]:
        """Detect fragments that semantically describe abandonment (FEAT-018).

        Two-stage pipeline: embedding-similarity gate → optional LLM
        verifier. Paradox-tagged and intimate-tier fragments are
        skipped here so neither the embedding nor the verifier sees
        them — important because the verifier may call out to a cloud
        LLM and intimate content must never leave the device.
        """
        if self._similarity_fn is None:
            return []
        candidates: list[CompostCandidate] = []
        for fragment in fragments:
            if self._skip_paradox and _is_paradox(fragment):
                continue
            if self._skip_intimate and _is_intimate(fragment):
                continue
            body = bodies.get(fragment.id, "")
            text = f"{fragment.title}\n{body}".strip() if body else fragment.title
            similarity = self._similarity_fn(text)
            if similarity < self._embedding_threshold:
                continue
            candidate = self._verify_fragment_candidate(fragment, body, similarity)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _verify_fragment_candidate(
        self,
        fragment: Fragment,
        body: str,
        similarity: float,
    ) -> CompostCandidate | None:
        """Stage 2: route an above-threshold fragment through the verifier.

        When ``self._verifier`` is ``None`` the embedding gate alone
        accepts the candidate. Otherwise:

        * ``yes`` → canonical compost candidate;
        * ``ambiguous`` → review-queue candidate (``for_review=True``);
        * ``no`` → returns ``None`` (skip).
        """
        verdict: CompostVerdict
        reasoning: str | None
        if self._verifier is None:
            verdict = CompostVerdict.YES
            reasoning = None
        else:
            result = self._verifier.verify(title=fragment.title, body=body)
            verdict = result.verdict
            reasoning = result.reasoning
            if verdict == CompostVerdict.NO:
                return None
        is_energetic = _fragment_is_energetic(fragment)
        reason = (
            f"Embedding-similarity {similarity:.2f}"
            if reasoning is None
            else f"{reasoning} (similarity {similarity:.2f})"
        )
        return CompostCandidate(
            source_type="fragment",
            source_id=fragment.id,
            title=fragment.title,
            reason=reason,
            fragment_ids=[fragment.id],
            energy_fragment_ids=[fragment.id] if is_energetic else [],
            energy_excerpts=[fragment.title] if is_energetic else [],
            similarity=similarity,
            verifier_reasoning=reasoning,
            for_review=verdict == CompostVerdict.AMBIGUOUS,
        )

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
        """Return a project candidate for *tag*, or ``None`` if still active.

        Both sides of the silence comparison are timezone-aware before
        they meet: ``cutoff`` derives from the aware clock, and each
        fragment timestamp is passed through
        :func:`creek.time.ensure_aware`. A vault mixing naive and aware
        fragment timestamps — the ordinary result of round-tripping
        frontmatter without an offset — therefore ranks and compares
        instead of raising ``TypeError`` (issue #938; the underlying
        model-level normalisation is issue #976).
        """
        if len(frags) < self.project_min_fragments:
            return None
        # FEAT-031: measure silence against the authored-date precedence
        # so a project whose fragments were merely *ingested* recently
        # but authored long ago is still recognised as silent.
        # ``max`` compares its own candidates, so the normalisation has to
        # happen inside the generator rather than around it: without it a
        # single naive timestamp in the group raises before ``cutoff`` is
        # ever consulted.
        last_seen = max(ensure_aware(effective_authored_at(frag)) for frag in frags)
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
        *,
        review_queue_relpath: str = "10-Liminal/Compost/Review",
    ) -> Path:
        """Write a compost note for *candidate* into the vault.

        Canonical compost notes live at
        ``10-Liminal/Compost/<date>-<sanitised-title>.md``. Candidates
        whose verifier returned ``ambiguous`` (``candidate.for_review``
        is ``True``) instead route to *review_queue_relpath* so the
        operator can triage them before they are filed canonically.

        Frontmatter captures the source reference, composted date,
        reason, tags, and fragment links. The body carries the three
        required sections ("What it was", "Why it composted", "What
        energy remains") plus a trailing list of all related fragments.

        Args:
            candidate: The compost candidate to record.
            vault_path: Path to the root of the Obsidian vault.
            review_queue_relpath: Vault-relative path for ambiguous
                verifier verdicts. Defaults to
                ``"10-Liminal/Compost/Review"``; callers wiring config
                pass ``config.compost.review_queue_relpath``.

        Returns:
            Path to the written note.
        """
        if candidate.for_review:
            target_dir = vault_path / review_queue_relpath
        else:
            target_dir = vault_path / "10-Liminal" / "Compost"
        target_dir.mkdir(parents=True, exist_ok=True)

        today = self._now.date()
        metadata = self._build_metadata(candidate, today)
        body = self._render_body(candidate)
        post = frontmatter.Post(content=body)
        post.metadata.update(metadata)

        filename = f"{today.isoformat()}-{_sanitize_filename(candidate.title)}.md"
        note_path = target_dir / filename
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    @staticmethod
    def _build_metadata(
        candidate: CompostCandidate,
        today: date,
    ) -> dict[str, object]:
        """Build the frontmatter metadata dict for *candidate*."""
        tags = ["compost"]
        if candidate.for_review:
            tags.append("compost-review")
        metadata: dict[str, object] = {
            "type": "compost",
            "title": candidate.title,
            "composted_date": today.isoformat(),
            "reason": candidate.reason,
            "fragments": [f"[[{fid}]]" for fid in candidate.fragment_ids],
            "tags": tags,
        }
        if candidate.source_type == "thread":
            metadata["original_thread"] = f"[[{candidate.source_id}]]"
        elif candidate.source_type == "fragment":
            metadata["original_fragment"] = f"[[{candidate.source_id}]]"
        else:
            metadata["original_project"] = candidate.source_id
        if candidate.similarity is not None:
            metadata["embedding_similarity"] = round(candidate.similarity, 4)
        if candidate.verifier_reasoning is not None:
            metadata["verifier_reasoning"] = candidate.verifier_reasoning
        return metadata

    @staticmethod
    def _render_body(candidate: CompostCandidate) -> str:
        """Render the markdown body for *candidate*."""
        lines: list[str] = []
        lines.extend(("## What it was", ""))
        lines.extend(
            (
                f"A {candidate.source_type} titled **{candidate.title}** that "
                "has since composted back into the vault.",
                "",
                "## Why it composted",
                "",
                candidate.reason or _UNKNOWN_REASON,
                "",
                "## What energy remains",
                "",
            )
        )
        if candidate.energy_fragment_ids:
            for fid in candidate.energy_fragment_ids:
                lines.append(f"- [[{fid}]]")
            if candidate.energy_excerpts:
                lines.append("")
                for excerpt in candidate.energy_excerpts:
                    lines.append(f"> {excerpt}")
        else:
            lines.append("_No crystallised insights were flagged for preservation._")
        lines.extend(("", "## Related Fragments", ""))
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
        lines.extend(
            (
                "Use this list to notice when composted ideas feed new growth:",
                "",
            )
        )
        if active_threads:
            for title, thread_id in active_threads:
                lines.append(f"- [[{thread_id}|{title}]]")
        else:
            lines.append("_No active threads recorded yet._")
        lines.append("")
        return "\n".join(lines)
