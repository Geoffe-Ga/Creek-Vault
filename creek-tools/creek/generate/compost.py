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
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from creek.generate.compost_verifier import SupportsVerifyCompost

logger = logging.getLogger(__name__)

CANONICAL_RELDIR: str = "10-Liminal/Compost"
"""Vault-relative folder holding confirmed compost notes.

Lives beside the code that writes into it so the scaffold drift guard
can derive the directory ``creek init`` must ship (#1025).
:mod:`creek.generate.compost_scan` re-exports it for its own callers."""

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

_SOURCE_KEYS: tuple[str, ...] = (
    "original_fragment",
    "original_thread",
    "original_project",
)
"""Frontmatter keys naming the source a compost note was written for.

``CompostTracker._build_metadata`` writes exactly one of these per note,
chosen by ``source_type``. Reading all three back is what makes a re-scan
idempotent across all three candidate kinds.
"""

_WIKILINK_RE = re.compile(r"\[\[(.+)\]\]")
"""Matches the ``[[id]]`` form used for fragment and thread back-references.

Project sources are stored bare (a tag, not a note), so unwrapping has to
tolerate both shapes.
"""

_MAX_FILENAME_ORDINALS: int = 10_000
"""Cap on the paths :func:`_resolve_compost_note_path` probes.

Counts the unsuffixed name, so a cap of 3 means ``stem``, ``stem-1``,
``stem-2``. Mirrors ``creek.vault.writer._MAX_FILENAME_COLLISION_RETRIES``:
high enough that no real vault reaches it, low enough that a runaway
collision pattern surfaces as a loud ``RuntimeError`` rather than an
infinite loop. The probe is
dearer than the writer's — it parses each occupant rather than attempting an
exclusive create — so a stem shared by *n* identities costs O(n²) reads across
the batch. That is the same cost profile the writer already accepts, and it
only bites a vault that has already gone degenerate.
"""


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


def _unwrap_source_id(value: object) -> str:
    """Return the bare source ID from a ``[[wikilink]]`` or plain string."""
    text = str(value).strip()
    match = _WIKILINK_RE.fullmatch(text)
    return match.group(1) if match else text


def _fragment_thread_titles(fragment: Fragment) -> list[str]:
    """Extract wiki-linked thread titles from a fragment's ``threads`` list."""
    return [_unwrap_source_id(raw) for raw in fragment.threads]


def _is_paradox(fragment: Fragment) -> bool:
    """Return whether *fragment* is marked paradoxical (issue #1210).

    Reads ``emotional_texture``, which is where the ontology spec puts
    it: "Do not resolve paradoxes or contradictions — tag them with
    ``paradox`` in emotional_texture"
    (``docs/Ontology/creek_ontology_agent_prompt.md`` line 715). That
    line is a mandate rather than a suggestion, and the classifier
    prompt has honoured it since #878 —
    :data:`creek.classify.llm.prompts.EMOTIONAL_TEXTURE_VOCABULARY`
    lists ``paradox`` and cites the same line. This reader was the last
    place still looking somewhere else.

    ``tags`` is deliberately **not** consulted as well. Nothing writes a
    fragment-level ``paradox`` tag on purpose; the only way one appeared
    was a literal ``#paradox`` hashtag harvested out of body text.
    Spec §10.2 does put ``#paradox`` on a *note* — the derived note in
    ``10-Liminal/Paradoxes/`` that
    :mod:`creek.generate.paradox` writes — which is a different object
    from the fragments it links, and is left alone.
    """
    return "paradox" in fragment.emotional_texture


def _is_intimate(fragment: Fragment) -> bool:
    """Return whether *fragment* carries the intimate privacy tier."""
    return fragment.privacy_tier == PrivacyTier.INTIMATE


@dataclass(frozen=True)
class _AdmittedFragments:
    """The fragment sequence after withheld fragments have been screened out.

    A distinct type rather than a bare list, so that a detector which has
    not been screened is a mypy ``arg-type`` error rather than a privacy
    leak found in production. Every private ``_detect_*`` method on
    :class:`CompostTracker` takes one of these; nothing else may.

    Attributes:
        fragments: The admitted fragments, in input order. These are the
            only fragments any detector may read — their IDs, titles and
            timestamps are the only ones that may reach a compost note.
        withheld_thread_titles: Titles of every thread linked by a
            *withheld* fragment. Deliberately titles and not the
            ``Fragment`` objects: the thread detector is the one that
            copies ``frag.title`` and ``frag.id`` into a note, so handing
            it withheld fragments would keep the leak one edit away. It is
            also what makes suppression O(1) per thread instead of
            re-parsing every withheld fragment's wikilinks per thread.
    """

    fragments: tuple[Fragment, ...]
    withheld_thread_titles: frozenset[str]


def _sanitize_filename(title: str) -> str:
    """Sanitise a title for use as part of a filename."""
    cleaned = re.sub(r"[^\w\s-]", "", title).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:80] or "compost"


def _candidate_source_pair(candidate: CompostCandidate) -> tuple[str, str]:
    """Return the frontmatter ``(key, value)`` pair identifying *candidate*.

    The single place ``source_type`` is mapped onto one of
    :data:`_SOURCE_KEYS`. :meth:`CompostTracker._build_metadata` writes the
    pair; :func:`_compost_source_of` reads it back; the note-path resolver
    compares them. Thread and fragment sources are wiki-linked because they
    are notes; a project source is a bare tag.

    Args:
        candidate: The compost candidate about to be written.

    Returns:
        The source key and the value exactly as it is written to disk.
    """
    if candidate.source_type == "thread":
        return ("original_thread", f"[[{candidate.source_id}]]")
    if candidate.source_type == "fragment":
        return ("original_fragment", f"[[{candidate.source_id}]]")
    return ("original_project", candidate.source_id)


def _compost_source_of(post: frontmatter.Post) -> tuple[str, str] | None:
    """Return the ``(source key, raw value)`` pair *post* records, or ``None``.

    ``None`` means "this note's identity cannot be positively established as
    a compost source" and covers two shapes: the note is not
    ``type: compost`` at all (the ``_Compost-Report.md`` shell, or an
    unrelated note that happened to want the same filename), or it carries
    none of :data:`_SOURCE_KEYS`.

    The value is returned **as written**, wrapper and all. Comparing through
    :func:`_unwrap_source_id` would collapse a fragment with id ``X`` and a
    project tagged ``X`` onto one identity, and those are two different
    things that must not overwrite each other (#1334).

    Args:
        post: A parsed markdown note.

    Returns:
        The first recorded source pair, or ``None``.
    """
    if post.get("type") != "compost":
        return None
    for key in _SOURCE_KEYS:
        raw = post.get(key)
        if raw is not None:
            return (key, str(raw))
    return None


def _recorded_compost_source(note: Path) -> tuple[str, str] | None:
    """Return the source pair a compost note on disk records, or ``None``.

    The disk-facing companion to :func:`_compost_source_of`, and the single
    extractor both the note writer and
    :func:`creek.generate.compost_scan.load_composted_source_ids` resolve
    identity through — so the index and the writer can never disagree about
    who owns what (#1334). A note that cannot be read or parsed is reported
    as ``None`` (identity unestablished) rather than raising, which is what
    makes the resolver step over a damaged neighbour instead of clobbering
    it.

    Args:
        note: Path to a candidate markdown note.

    Returns:
        The recorded ``(source key, raw value)`` pair, or ``None``.
    """
    try:
        post = frontmatter.load(str(note))
    except FRONTMATTER_LOAD_ERRORS:
        logger.debug("Skipping unreadable compost note: %s", note)
        return None
    return _compost_source_of(post)


def _resolve_compost_note_path(
    target_dir: Path,
    stem: str,
    identity: tuple[str, str],
) -> Path:
    """Return the path *identity*'s note owns under *target_dir*.

    Probes ``stem.md``, ``stem-1.md``, ``stem-2.md``, … and returns the first
    path that is either free or already occupied by a note recording
    *identity*. A path occupied by anything else — a different source, or a
    note whose identity :func:`_recorded_compost_source` cannot establish —
    is skipped, never overwritten.

    Deliberately *not* ``VaultWriter._atomic_create``: that helper advances on
    occupancy, so it would mint a new file every run and never converge. This
    one advances on ownership, so the file count is bounded by the number of
    distinct identities.

    Only exact paths are probed — never ``glob``/``iterdir``, which would make
    a batch quadratic in directory size at 35k-fragment scale.

    Args:
        target_dir: Directory the note will be written into.
        stem: Filename stem (without ``.md``) the candidate naturally wants.
        identity: The candidate's ``(source key, raw value)`` pair.

    Returns:
        The path to write to.

    Raises:
        RuntimeError: If every one of :data:`_MAX_FILENAME_ORDINALS` probed
            paths belongs to someone else.
    """
    # The ordinal loop is duplicated in creek/generate/decisions.py on
    # purpose. Hoisting a shared filename builder is explicitly out of scope
    # for #1334 and is issue #1417's job; merging them here would pre-empt it.
    for ordinal in range(_MAX_FILENAME_ORDINALS):
        suffix = "" if ordinal == 0 else f"-{ordinal}"
        candidate_path = target_dir / f"{stem}{suffix}.md"
        if (
            not candidate_path.exists()
            or _recorded_compost_source(candidate_path) == identity
        ):
            return candidate_path
    msg = (
        f"Could not allocate a unique filename for '{stem}.md' in "
        f"{target_dir} after {_MAX_FILENAME_ORDINALS} attempts"
    )
    raise RuntimeError(msg)


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
            skip_paradox: When ``True``, fragments carrying
                ``paradox`` in ``emotional_texture`` — the field the
                ontology spec mandates, read there since issue #1210 —
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

        **Withheld fragments are screened out once, here, before any
        detector runs.** Paradox-textured fragments (``skip_paradox``) and
        intimate-tier fragments (``skip_intimate``) are removed by
        :meth:`_screen`, which is the single chokepoint: a withheld
        fragment contributes no ID, no title, no excerpt, no count and no
        date to any candidate, and — because the screen happens before
        :meth:`_group_fragments_by_tag` — cannot bring a project identity
        into existence. Screening at the boundary rather than inside each
        detector is load-bearing, not tidiness: a project candidate's
        ``source_id`` and ``title`` *are* the tag, which reaches the note's
        filename and ``_Compost-Report.md``, so filtering the emitted
        ``fragment_ids`` lists would leave a tag carried only by intimate
        fragments naming a file in the vault (issue #1311).

        Three consequences are deliberate:

        * **Silent omission.** A thread named only by withheld fragments
          yields no candidate at all. Emitting one with
          ``_No related fragments were recorded._`` in place of its
          fragment list would announce that the thread's only fragments are
          protected — which is the fact being protected. Suppression is
          conditioned on withheld-ness, never on emptiness: a thread whose
          fragments simply have not been ingested still composts.
        * **A project alive only in withheld fragments falls silent.**
          Silence is measured over the admitted set, so such a project now
          produces a compost note it would not have produced before. This
          is accepted: no withheld fragment becomes admitted, and the note
          is derived entirely from admitted fragments.
        * **The thread reason is an accepted residual channel.**
          :meth:`_thread_reason` reads ``Thread.last_seen`` from the
          thread's own frontmatter, not from fragments, so a dormancy
          window stamped by a withheld fragment survives screening.
          ``02-Threads/<id>.md`` is an ordinary vault note, so this
          discloses nothing a reader could not already see.

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
        admitted = self._screen(fragments)
        thread_candidates = self._detect_threads(threads, admitted)
        fragment_candidates = self._detect_abandonment_fragments(
            admitted,
            fragment_bodies or {},
        )
        project_candidates = self._detect_disappeared_projects(admitted)
        return [*thread_candidates, *fragment_candidates, *project_candidates]

    def _is_withheld(self, fragment: Fragment) -> bool:
        """Return whether *fragment* is excluded from compost detection.

        The single predicate behind both skip policies. Keeping the
        ``paradox`` and ``intimate`` tests in one place is what made
        issue #1210 — which moved paradox detection from ``tags`` to
        ``emotional_texture`` — a change to one expression rather than to
        every detector. See :func:`_is_paradox` for why the old field is
        not read alongside the new one.
        """
        if self._skip_paradox and _is_paradox(fragment):
            return True
        return self._skip_intimate and _is_intimate(fragment)

    def _screen(self, fragments: list[Fragment]) -> _AdmittedFragments:
        """Partition *fragments* into what detection may see and what it may not.

        The one chokepoint enforcing ``skip_paradox`` / ``skip_intimate``.
        Runs before every detector, so nothing downstream needs to know the
        policies exist — and a detector added later is guarded by
        construction. Withheld fragments are reduced immediately to the set
        of thread titles they mention; no withheld :class:`Fragment` object
        travels any further.

        Args:
            fragments: Every fragment loaded for this detection pass.

        Returns:
            The admitted fragments, plus every thread title any withheld
            fragment names. That set is deliberately not "titles *only*
            withheld fragments name": a thread also named by an admitted
            fragment appears in it too, and :meth:`_thread_candidate`
            draws the distinction by checking the set only when no
            admitted fragment turned out to be related.
        """
        admitted: list[Fragment] = []
        withheld_thread_titles: set[str] = set()
        for fragment in fragments:
            if self._is_withheld(fragment):
                withheld_thread_titles.update(_fragment_thread_titles(fragment))
            else:
                admitted.append(fragment)
        return _AdmittedFragments(
            fragments=tuple(admitted),
            withheld_thread_titles=frozenset(withheld_thread_titles),
        )

    def _detect_threads(
        self,
        threads: list[Thread],
        admitted: _AdmittedFragments,
    ) -> list[CompostCandidate]:
        """Detect compost candidates whose source is a thread.

        Mirrors :meth:`_detect_disappeared_projects`: the per-source
        decision lives in :meth:`_thread_candidate`, and this method only
        collects what that returns.
        """
        today = self._now.date()
        candidates: list[CompostCandidate] = []
        for thread in threads:
            candidate = self._thread_candidate(thread, admitted, today)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _thread_candidate(
        self,
        thread: Thread,
        admitted: _AdmittedFragments,
        today: date,
    ) -> CompostCandidate | None:
        """Return a candidate for *thread*, or ``None`` if it is not compost.

        Two ways to return ``None``. The thread may simply not be dormant
        or resolved. Or every fragment naming it may have been withheld, in
        which case the candidate is omitted rather than written with an
        empty fragment list — see the silent-omission paragraph on
        :meth:`detect_compost_candidates`. That second condition tests
        withheld-ness, never emptiness: a thread whose fragments have not
        been ingested yet has no withheld fragments either, and must still
        compost.
        """
        reason = self._thread_reason(thread, today)
        if reason is None:
            return None
        related = self._related_fragments(thread, admitted)
        if not related and thread.title in admitted.withheld_thread_titles:
            return None
        energetic = [frag for frag in related if _fragment_is_energetic(frag)]
        return CompostCandidate(
            source_type="thread",
            source_id=thread.id,
            title=thread.title,
            reason=reason,
            fragment_ids=[frag.id for frag in related],
            energy_fragment_ids=[frag.id for frag in energetic],
            energy_excerpts=[frag.title for frag in energetic],
        )

    @staticmethod
    def _related_fragments(
        thread: Thread,
        admitted: _AdmittedFragments,
    ) -> list[Fragment]:
        """Return the admitted fragments that wiki-link *thread* by title.

        Reads ``admitted.fragments`` and nothing else, so a withheld
        fragment cannot be "related" to anything.
        """
        return [
            frag
            for frag in admitted.fragments
            if thread.title in _fragment_thread_titles(frag)
        ]

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
        admitted: _AdmittedFragments,
        bodies: Mapping[str, str],
    ) -> list[CompostCandidate]:
        """Detect fragments that semantically describe abandonment (FEAT-018).

        Two-stage pipeline: embedding-similarity gate → optional LLM
        verifier. Neither the embedding gate nor the verifier can see a
        paradox-tagged or intimate-tier fragment, because :meth:`_screen`
        removed it upstream in :meth:`detect_compost_candidates` — the
        guarantee is now structural rather than a check inside this loop.
        That matters because the verifier may call out to a cloud LLM and
        intimate content must never leave the device.
        """
        if self._similarity_fn is None:
            return []
        candidates: list[CompostCandidate] = []
        for fragment in admitted.fragments:
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
        admitted: _AdmittedFragments,
    ) -> list[CompostCandidate]:
        """Detect tagged projects that have fallen silent.

        Grouping runs over the admitted fragments only, so a tag carried
        exclusively by withheld fragments never becomes a project identity
        — which is the leak channel that filtering a candidate's
        ``fragment_ids`` cannot close, since the identity *is* the tag.
        Both the ``project_min_fragments`` count and the silence date are
        therefore computed from admitted fragments alone.
        """
        cutoff = self._now - timedelta(days=self.project_gap_days)
        tag_fragments = self._group_fragments_by_tag(admitted.fragments)
        candidates: list[CompostCandidate] = []
        for tag, frags in tag_fragments.items():
            candidate = self._project_candidate(tag, frags, cutoff)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _group_fragments_by_tag(
        fragments: Sequence[Fragment],
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
        fragment timestamp comes back aware from
        :func:`creek.time.effective_authored_at`, which anchors a naive
        value to America/Los_Angeles at the read (#1116). A vault mixing
        naive and aware fragment timestamps — the ordinary result of
        round-tripping frontmatter without an offset — therefore ranks
        and compares instead of raising ``TypeError`` (issue #938; the
        model-level normalisation this backs up is issue #976).

        This method used to wrap that call in its own
        :func:`creek.time.ensure_aware`, because the chokepoint did not
        repair. It now does, so the wrapper was dropped as a
        value-for-value no-op — both applied the same LA anchor, so no
        candidate's ``last_seen`` moves. The guarantee still has to hold
        *inside* the generator, because ``max`` compares its own
        candidates and one unrepaired naive timestamp raises before
        ``cutoff`` is ever consulted; that is what
        ``tests/test_compost.py::TestTimezoneAwareClock::
        test_bypass_naive_fragment_still_ranks`` pins.
        """
        if len(frags) < self.project_min_fragments:
            return None
        # FEAT-031: measure silence against the authored-date precedence
        # so a project whose fragments were merely *ingested* recently
        # but authored long ago is still recognised as silent.
        last_seen = max(effective_authored_at(frag) for frag in frags)
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
        ``10-Liminal/Compost/<date>-<sanitised-title>[-N].md``. Candidates
        whose verifier returned ``ambiguous`` (``candidate.for_review``
        is ``True``) instead route to *review_queue_relpath* so the
        operator can triage them before they are filed canonically.

        Frontmatter captures the source reference, composted date,
        reason, tags, and fragment links. The body carries the three
        required sections ("What it was", "Why it composted", "What
        energy remains") plus a trailing list of all related fragments.

        ``[-N]`` is the collision ordinal. The stem is derived from the
        title alone, so two different sources can want it — the sanitiser's
        80-character truncation manufactures collisions by itself, and every
        untitled candidate of a given day wants ``<date>-compost.md`` — so
        the path is resolved by *ownership*:
        :func:`_resolve_compost_note_path` probes ``-1``, ``-2``, … until it
        finds a path that is free or already records this candidate's
        ``(source key, source id)`` pair. **A note recording a different
        source is never overwritten** (#1334). A note this candidate already
        owns *is* rewritten in place; that refresh is deliberate, and is what
        the index gate in
        :func:`~creek.generate.compost_scan.run_compost_scan` keeps
        production from reaching.

        "Already owns" means *at today's stem*. The stem embeds the
        tracker's clock date, so a note this candidate owns from an earlier
        day is never probed, and a direct call on a later day writes a
        second note rather than refreshing the first — the same caveat
        ``creek/generate/paradox.py`` carries (#1320). The index gate spans
        every date, so production is unaffected.

        The disambiguator is a bare integer on purpose. A project
        candidate's ``source_id`` *is* its tag, which can be derived from
        intimate fragments, so no identity-derived string may be added to a
        filename to keep two notes apart; an ordinal discloses nothing but a
        collision count (#1311 / PR #1404).

        Args:
            candidate: The compost candidate to record.
            vault_path: Path to the root of the Obsidian vault.
            review_queue_relpath: Vault-relative path for ambiguous
                verifier verdicts. Defaults to
                ``"10-Liminal/Compost/Review"``; callers wiring config
                pass ``config.compost.review_queue_relpath``.

        Returns:
            Path to the written note.

        Raises:
            RuntimeError: If :data:`_MAX_FILENAME_ORDINALS` consecutive
                candidate filenames all belong to other sources.
        """
        if candidate.for_review:
            target_dir = vault_path / review_queue_relpath
        else:
            target_dir = vault_path / CANONICAL_RELDIR
        target_dir.mkdir(parents=True, exist_ok=True)

        today = self._now.date()
        metadata = self._build_metadata(candidate, today)
        body = self._render_body(candidate)
        post = frontmatter.Post(content=body)
        post.metadata.update(metadata)

        stem = f"{today.isoformat()}-{_sanitize_filename(candidate.title)}"
        note_path = _resolve_compost_note_path(
            target_dir,
            stem,
            _candidate_source_pair(candidate),
        )
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    @staticmethod
    def _build_metadata(
        candidate: CompostCandidate,
        today: date,
    ) -> dict[str, object]:
        """Build the frontmatter metadata dict for *candidate*.

        The source key/value comes from :func:`_candidate_source_pair`, the
        same function the note-path resolver compares against, so the writer
        and every reader of a note's identity cannot drift (#1334).
        """
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
        source_key, source_value = _candidate_source_pair(candidate)
        metadata[source_key] = source_value
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
        compost_dir = vault_path / CANONICAL_RELDIR
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
        """Return ``(title, filename)`` tuples for compost notes in *compost_dir*.

        ``yaml.YAMLError`` joins the skip tuple because #1334 made an
        unparseable neighbour *survivable*: the note-path resolver now steps
        over a note whose identity it cannot establish instead of
        overwriting it. That is the right call, but it means a malformed
        note persists in ``10-Liminal/Compost/`` where it used to be
        clobbered — so this scan, which ``creek fill`` runs over the same
        folder, would newly crash on it. Same bug class as issue #1416.
        """
        notes: list[tuple[str, str]] = []
        for md_file in sorted(compost_dir.glob("*.md")):
            if md_file == report_path:
                continue
            try:
                post = frontmatter.load(str(md_file))
            except FRONTMATTER_LOAD_ERRORS:
                continue
            if post.get("type") != "compost":
                continue
            title = str(post.get("title") or md_file.stem)
            notes.append((title, md_file.stem))
        return notes

    @staticmethod
    def _load_active_threads(threads_dir: Path) -> list[tuple[str, str]]:
        """Return ``(title, id)`` tuples for active thread notes.

        Skips unparseable notes for the same reason as
        :meth:`_load_existing_compost_notes`: the two run in one pass from
        :meth:`generate_compost_report`, and a report that dies on one bad
        thread note is no more use than one that dies on a bad compost note.
        """
        if not threads_dir.exists():
            return []
        active: list[tuple[str, str]] = []
        for md_file in sorted(threads_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(md_file))
            except FRONTMATTER_LOAD_ERRORS:
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
