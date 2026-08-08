"""Vault-scanning compost detection — the engine behind ``creek compost scan``.

FEAT-018 built a two-stage compost detector (embedding gate → LLM verifier)
and #266 gave it a scoring harness, but no command ever pointed it at a
vault: ``creek compost`` exposed only ``calibrate``, which reads a labelled
YAML fixture. ``10-Liminal/Compost/`` was consequently unreachable — on the
35,330-fragment demo vault, with every command run, it held only
``.gitkeep``. This module closes that gap (issue #882).

Three invariants shape the design, and each is pinned by a test in
``tests/test_compost_scan.py``:

**The embedding gate never gets the last word.** ``CompostTracker`` treats a
``None`` verifier as "the gate alone accepts", which is right for calibration
but wrong for a command that writes to the vault. Here a fragment that clears
the gate but was not verified routes to the operator review queue, never to
canonical compost. An embedding hit is a suspicion, not a finding.

**Intimate content never reaches the verifier.** ``skip_intimate`` is forced
on rather than plumbed through :class:`~creek.config.CompostConfig`, so no
config edit or flag can send intimate fragment bodies to a cloud provider.
``creek/cli.py``'s ``_build_compost_verifier`` carries a durable caveat about
exactly this call site; ``creek.cli._build_scan_verifier`` is the tier-aware
builder that caveat asks for.

**Costs are quoted before they are incurred.** Detection runs gate-only
first, so the candidate count — and therefore the LLM call count — is known
and printable before a single verification request goes out.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.generate.compost import CompostTracker
from creek.generate.compost_verifier import CompostVerdict
from creek.models import Thread
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from creek.config import CompostConfig
    from creek.generate.compost import CompostCandidate
    from creek.generate.compost_verifier import SupportsVerifyCompost
    from creek.models import Fragment

logger = logging.getLogger(__name__)

CANONICAL_RELDIR: str = "10-Liminal/Compost"
"""Vault-relative folder holding confirmed compost notes."""

_FRAGMENTS_RELDIR: str = "01-Fragments"
"""Vault-relative folder the scan reads fragments from."""

_THREADS_RELDIR: str = "02-Threads"
"""Vault-relative folder the scan reads threads from."""

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


@dataclass(frozen=True)
class CompostScanPlan:
    """Pre-flight estimate of the work a scan will do.

    Produced from the gate-only detection pass, before any verification
    request is made, so an operator can see the cost and bail.

    Attributes:
        fragment_candidates: Fragments that cleared the embedding gate and
            are not already composted.
        thread_candidates: Dormant or resolved threads found.
        project_candidates: Tagged projects that have fallen silent.
        llm_calls: Verification requests the scan will issue — equal to
            ``fragment_candidates`` when a verifier is wired, otherwise 0.
            Thread and project detection is deterministic and never calls
            an LLM.
    """

    fragment_candidates: int
    thread_candidates: int
    project_candidates: int
    llm_calls: int


@dataclass(frozen=True)
class CompostScanResult:
    """Outcome of one :func:`run_compost_scan` invocation.

    Attributes:
        plan: The pre-flight estimate the run was based on.
        composted: Paths of notes written to :data:`CANONICAL_RELDIR`.
        review_queued: Paths of notes written to the review queue.
        skipped_existing: Candidates dropped because the vault already
            holds a compost note for that source — the idempotency count.
    """

    plan: CompostScanPlan
    composted: list[Path]
    review_queued: list[Path]
    skipped_existing: int


def _unwrap_source_id(value: object) -> str:
    """Return the bare source ID from a ``[[wikilink]]`` or plain string."""
    text = str(value).strip()
    match = _WIKILINK_RE.fullmatch(text)
    return match.group(1) if match else text


def _safe_post(md_file: Path) -> frontmatter.Post | None:
    """Return a parsed frontmatter post, or ``None`` when it will not parse."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown file: %s", md_file)
        return None


def load_composted_source_ids(
    vault_path: Path,
    *,
    review_queue_relpath: str,
) -> set[str]:
    """Return every source ID the vault already holds a compost note for.

    Spans both the canonical folder and the review queue: a candidate
    awaiting operator triage has already been surfaced, so re-filing it on
    the next scan would duplicate work and re-spend an LLM call. Notes that
    are unreadable, are not ``type: compost``, or carry none of the source
    keys are ignored — which is how the ``_Compost-Report.md`` shell
    (``type: compost-report``) stays out of the index.

    Args:
        vault_path: Root of the Obsidian vault.
        review_queue_relpath: Vault-relative review-queue path, normally
            ``config.compost.review_queue_relpath``.

    Returns:
        The set of ``source_id`` values already recorded. Empty for a vault
        that has never been scanned.
    """
    seen: set[str] = set()
    folders = (vault_path / CANONICAL_RELDIR, vault_path / review_queue_relpath)
    for folder in folders:
        if not folder.exists():
            continue
        for md_file in sorted(folder.rglob("*.md")):
            post = _safe_post(md_file)
            if post is None or post.get("type") != "compost":
                continue
            for key in _SOURCE_KEYS:
                raw = post.get(key)
                if raw is not None:
                    seen.add(_unwrap_source_id(raw))
                    break
    return seen


def _load_fragments(vault_path: Path) -> tuple[list[Fragment], dict[str, str]]:
    """Load every fragment under ``01-Fragments`` plus its body text.

    The bodies matter: the embedding gate scores ``title + body``, and the
    verifier is shown both. :func:`creek.vault.reader.iter_vault_fragments`
    is the only loader that returns them alongside the validated model.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        ``(fragments, {fragment_id: body})``.
    """
    records = iter_vault_fragments(vault_path / _FRAGMENTS_RELDIR)
    fragments = [fragment for _path, fragment, _body, _raw in records]
    bodies = {fragment.id: body for _path, fragment, body, _raw in records}
    return fragments, bodies


def _load_threads(vault_path: Path) -> list[Thread]:
    """Load every thread under ``02-Threads``, skipping unparseable notes."""
    root = vault_path / _THREADS_RELDIR
    if not root.exists():
        return []
    threads: list[Thread] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "thread":
            continue
        try:
            threads.append(Thread.model_validate(metadata))
        except ValidationError:
            logger.debug("Skipping invalid thread frontmatter: %s", md_file)
            continue
    return threads


def _build_plan(
    candidates: Sequence[CompostCandidate],
    *,
    verifying: bool,
) -> CompostScanPlan:
    """Summarise *candidates* into a pre-flight plan.

    Args:
        candidates: Gate-only candidates, already filtered against the
            idempotency index.
        verifying: Whether a verifier is wired. Drives ``llm_calls``.

    Returns:
        The populated :class:`CompostScanPlan`.
    """
    counts = Counter(candidate.source_type for candidate in candidates)
    fragment_candidates = counts["fragment"]
    return CompostScanPlan(
        fragment_candidates=fragment_candidates,
        thread_candidates=counts["thread"],
        project_candidates=counts["project"],
        llm_calls=fragment_candidates if verifying else 0,
    )


def _verified_reason(candidate: CompostCandidate, reasoning: str | None) -> str:
    """Compose the note's reason line from the verifier's explanation.

    Mirrors the format ``CompostTracker._verify_fragment_candidate``
    produces, so a note written by the scan is indistinguishable from one
    written by an in-tracker verification.
    """
    if reasoning is None or candidate.similarity is None:
        return candidate.reason
    return f"{reasoning} (similarity {candidate.similarity:.2f})"


def _route(
    candidates: Sequence[CompostCandidate],
    *,
    verifier: SupportsVerifyCompost | None,
    bodies: Mapping[str, str],
) -> list[CompostCandidate]:
    """Decide each candidate's destination folder.

    Thread and project candidates pass through untouched — their reasons are
    arithmetic (dormancy windows, silence gaps), so there is nothing for an
    LLM to confirm and nothing for an operator to triage.

    Fragment candidates depend on the verifier:

    * no verifier (``--no-llm``) → ``for_review=True``, review queue. The
      gate alone is a suspicion, and filing a suspicion as canonical compost
      is the dishonesty #882 exists to remove;
    * ``yes`` → canonical compost;
    * ``ambiguous`` → review queue;
    * ``no`` → dropped.

    Args:
        candidates: Fresh (not already composted) gate-only candidates.
        verifier: The LLM verifier, or ``None`` under ``--no-llm``.
        bodies: ``{fragment_id: body}`` for the verifier prompt.

    Returns:
        Candidates to write, each with ``for_review`` set correctly.
    """
    routed: list[CompostCandidate] = []
    for candidate in candidates:
        if candidate.source_type != "fragment":
            routed.append(candidate)
            continue
        if verifier is None:
            routed.append(replace(candidate, for_review=True))
            continue
        result = verifier.verify(
            title=candidate.title,
            body=bodies.get(candidate.source_id, ""),
        )
        if result.verdict == CompostVerdict.NO:
            continue
        routed.append(
            replace(
                candidate,
                reason=_verified_reason(candidate, result.reasoning),
                verifier_reasoning=result.reasoning,
                for_review=result.verdict == CompostVerdict.AMBIGUOUS,
            ),
        )
    return routed


def _write_notes(
    candidates: Sequence[CompostCandidate],
    *,
    tracker: CompostTracker,
    vault_path: Path,
    review_queue_relpath: str,
) -> tuple[list[Path], list[Path]]:
    """Write each routed candidate's note and split the paths by destination.

    Args:
        candidates: Routed candidates, each with ``for_review`` already set.
        tracker: The tracker whose clock datestamps the notes.
        vault_path: Root of the Obsidian vault.
        review_queue_relpath: Vault-relative review-queue path.

    Returns:
        ``(composted, review_queued)`` — the paths written to each folder.
    """
    composted: list[Path] = []
    review_queued: list[Path] = []
    for candidate in candidates:
        note_path = tracker.create_compost_note(
            candidate,
            vault_path,
            review_queue_relpath=review_queue_relpath,
        )
        target = review_queued if candidate.for_review else composted
        target.append(note_path)
    return composted, review_queued


def run_compost_scan(
    vault_path: Path,
    *,
    similarity_fn: Callable[[str], float],
    config: CompostConfig,
    verifier: SupportsVerifyCompost | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> CompostScanResult:
    """Scan *vault_path* for compost candidates and write their notes.

    Runs in two phases so the cost is quotable before it is spent:

    1. **Gate.** ``CompostTracker`` runs with ``verifier=None``, producing
       thread, fragment, and project candidates. Candidates whose source
       already has a compost note are dropped here and counted in
       ``skipped_existing`` — which is what makes a re-scan idempotent and
       keeps a second run from re-spending LLM calls.
    2. **Verify and write.** Survivors are routed by :func:`_route` and
       written via
       :meth:`~creek.generate.compost.CompostTracker.create_compost_note`,
       which already handles canonical-vs-review-queue placement.

    ``skip_intimate`` is forced on regardless of *config*: this function
    feeds fragment bodies to a potentially cloud-hosted verifier, and the
    Intimate-never-cloud rule must not be defeasible by a config edit.

    Args:
        vault_path: Root of the Obsidian vault.
        similarity_fn: Text → max cosine similarity against the compost
            exemplars (see
            :func:`creek.generate.compost_embedding.make_similarity_fn`).
        config: Compost settings — embedding floor, paradox skipping, and
            the review-queue path.
        verifier: LLM verifier, or ``None`` for a gate-only (``--no-llm``)
            run in which no fragment content leaves the device.
        now: Reference clock for dormancy maths and note datestamps.
            Defaults to :func:`creek.time.now_la` via ``CompostTracker``.
        dry_run: When ``True``, return the plan and write nothing. No
            verification request is issued either.

    Returns:
        A :class:`CompostScanResult` describing what was planned and written.
    """
    fragments, bodies = _load_fragments(vault_path)
    threads = _load_threads(vault_path)

    tracker = CompostTracker(
        now=now,
        similarity_fn=similarity_fn,
        verifier=None,
        embedding_threshold=config.embedding_threshold,
        skip_paradox=config.skip_paradox,
        skip_intimate=True,
    )
    candidates = tracker.detect_compost_candidates(
        threads,
        fragments,
        fragment_bodies=bodies,
    )

    seen = load_composted_source_ids(
        vault_path,
        review_queue_relpath=config.review_queue_relpath,
    )
    fresh = [c for c in candidates if c.source_id not in seen]
    skipped_existing = len(candidates) - len(fresh)
    plan = _build_plan(fresh, verifying=verifier is not None)

    if dry_run:
        return CompostScanResult(
            plan=plan,
            composted=[],
            review_queued=[],
            skipped_existing=skipped_existing,
        )

    composted, review_queued = _write_notes(
        _route(fresh, verifier=verifier, bodies=bodies),
        tracker=tracker,
        vault_path=vault_path,
        review_queue_relpath=config.review_queue_relpath,
    )
    return CompostScanResult(
        plan=plan,
        composted=composted,
        review_queued=review_queued,
        skipped_existing=skipped_existing,
    )
