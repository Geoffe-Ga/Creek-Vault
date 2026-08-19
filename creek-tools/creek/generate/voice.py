"""Voice exemplar collection and pattern extraction.

Implements Section 11.1 of the Creek Ontology.

**Exemplar collection** — :class:`VoiceExemplarCollector` scans the vault
for fragments whose ``voice.confidence`` has crystallised (``settled`` or
``conviction``), groups them by ``voice.register``, ranks them by quality,
and writes the top exemplars under ``07-Voice/Register-Samples/<register>/``
so a voice proxy can later train against curated samples.

**Pattern extraction** — :class:`VoicePatternExtractor` analyses exemplar
body texts to extract sentence structure, paragraph structure, transition
patterns, metaphor families, rhetorical moves, vocabulary fingerprint, and
punctuation habits.

**Voice fidelity** is guarded separately from privacy, by three additive
gates in :func:`_eligible_register` and the walks around it. The primary
one is authorship (#1213): a fragment whose ``source.author`` is not
``self`` is refused outright, which is what enforces the guarantee
:attr:`~creek.models.Fragment.voice_proxy_eligible` documents. Behind it
sit two backstops from #466 — ``voice_weight > 0``, stamped to ``0.0`` on
AI turns at ingest, and the ``11-Other-Authors`` path exclusion
(:data:`_OTHER_AUTHORS_SEGMENT`). Both are retained deliberately: they
catch a fragment whose authorship axis is missing or mis-set.

The collector is deliberately conservative about privacy, and two
independent gates say so. ``allow_intimate`` is a **consent** gate: the
voice proxy is opt-in even at ``--include-tier intimate``
(``creek/templates/skills/privacy-tier.SKILL.md:47``). ``override`` is a
**ceiling** gate (#968): the caller's declared
:class:`~creek.classify.privacy_filter.PrivacyTierOverride`, applied to each
note's raw frontmatter via
:func:`~creek.classify.privacy_filter.within_ceiling`. They are additive by
design — admission is ``allow_intimate`` *and* ``within_ceiling`` — and must
not be "simplified" into one.

Both gates read the same tier because :func:`_load_fragment_with_body`
materialises the **declared** tier onto every model it returns (#1212): a
note with no ``privacy_tier`` key resolves to ``intimate`` rather than to
the model's ``unclassified`` default, so it is refused wherever a
declared-intimate note is. Without that, the consent gate — which sees only
the model — admitted unvouched-for content whenever the ceiling gate was at
:attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL` and therefore
short-circuiting.

Saved exemplars include a per-register summary note recording the breakdown
of confidence levels and the manifest of the copies the run wrote
(:func:`_sample_digest`), which is what entitles the next run to delete one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
from pydantic import ValidationError

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    raw_privacy_tier,
    within_ceiling,
)
from creek.config import VoiceAudienceWeightingConfig
from creek.models import (
    Authorship,
    Confidence,
    Fragment,
    Frequency,
    Mode,
    Phase,
    PrivacyTier,
    VoiceRegister,
)
from creek.vault.links import read_header_meta
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

logger = logging.getLogger(__name__)

VOICE_REGISTERS: tuple[str, ...] = tuple(register.value for register in VoiceRegister)
"""All seven canonical voice registers from the Creek ontology."""

DEFAULT_MAX_PER_REGISTER: int = 20
"""Default cap on exemplars retained per register."""

DEFAULT_MIN_PER_REGISTER: int = 5
"""Registers with fewer than this many exemplars trigger a warning."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
"""Vault subdirectory scanned for fragment markdown files."""

_OTHER_AUTHORS_SEGMENT: str = "11-Other-Authors"
"""Vault path segment holding borrowed / AI-authored content (Issue #466).

Any fragment whose source path contains this segment is excluded from the
voice corpus regardless of its ``voice_weight``, as a defensive backstop
against voice drift.

Since #1213 this is a backstop to a real frontmatter gate rather than a
front-line defence, and it is **retained deliberately** — it still catches
a fragment whose ``source.author`` axis is absent or mis-set. It was never
load-bearing on its own: the walks only rglob :data:`_FRAGMENTS_SUBDIR`,
while :meth:`creek.vault.writer.VaultWriter._fragment_target_dir` routes
borrowed fragments to a vault-root ``11-Other-Authors/<slug>/`` that is
outside the scan root entirely. What this segment check actually catches
is a nested or symlinked occurrence *inside* ``01-Fragments/``.
"""

_SAMPLES_SUBPATH: tuple[str, str] = ("07-Voice", "Register-Samples")
"""Vault path where ranked exemplars are persisted."""

_QUALIFYING_CONFIDENCE: frozenset[str] = frozenset(
    {Confidence.SETTLED.value, Confidence.CONVICTION.value},
)
"""Confidence levels that make a fragment eligible as an exemplar."""

_CONFIDENCE_SCORE: dict[str, int] = {
    Confidence.CONVICTION.value: 3,
    Confidence.SETTLED.value: 2,
}
"""Per-confidence base score contribution to the ranking."""

_MEDIUM_LENGTH_MIN_WORDS: int = 200
"""Lower bound (inclusive) of the medium-length word band."""

_MEDIUM_LENGTH_MAX_WORDS: int = 800
"""Upper bound (inclusive) of the medium-length word band."""

_LENGTH_BONUS: int = 1
"""Score bonus applied to medium-length bodies."""

_CLASSIFICATION_BONUS: int = 1
"""Score bonus applied when every classification axis is populated."""

_SUMMARY_FILENAME: str = "_Summary.md"
"""Filename used for the per-register summary note."""

_SUMMARY_STEM: str = Path(_SUMMARY_FILENAME).stem
"""The summary's own stem, which no exemplar copy may claim.

Derived rather than restated so the reserved name cannot drift away from
the file it reserves.
"""

_MANIFEST_KEY: str = "exemplar_digests"
"""Summary frontmatter key holding one fingerprint per copy the run wrote."""

_ID_PATH_CHARS: frozenset[str] = frozenset({"/", "\\", "\x00"})
"""Characters that make a ``Fragment.id`` a path rather than a filename."""

_ID_DIRECTORY_NAMES: frozenset[str] = frozenset({"", ".", ".."})
"""Whole-id values that name a directory (or nothing) rather than a file."""

_NAME_MAX_BYTES: int = 255
"""POSIX ``NAME_MAX``: the byte length limit on a single filename.

255 on every filesystem creek runs on (APFS, ext4, XFS). The unit is bytes,
not characters, because that is what the kernel counts: a non-ASCII id
spends more of the budget than its length suggests.
"""


@dataclass(frozen=True)
class _ExemplarRecord:
    """Cached scoring data captured during ``collect_exemplars``.

    Attributes:
        word_count: Number of whitespace-delimited tokens in the body.
        source_path: Original markdown path of the fragment, used by
            :meth:`VoiceExemplarCollector.save_exemplars` to copy the
            file into the register samples folder.
    """

    word_count: int
    source_path: Path


def _confidence_value(fragment: Fragment) -> str:
    """Return the fragment's voice confidence as a string (or empty)."""
    return str(fragment.voice.confidence) if fragment.voice.confidence else ""


def _register_value(fragment: Fragment) -> str:
    """Return the fragment's voice register as a string (or empty)."""
    return str(fragment.voice.voice_register) if fragment.voice.voice_register else ""


def _is_fully_classified(fragment: Fragment) -> bool:
    """Return whether every primary classification axis is populated."""
    if str(fragment.frequency.primary) == Frequency.UNCLASSIFIED.value:
        return False
    if str(fragment.wavelength.phase) == Phase.UNCLASSIFIED.value:
        return False
    if str(fragment.wavelength.mode) == Mode.UNCLASSIFIED.value:
        return False
    if not fragment.voice.voice_register:
        return False
    return bool(fragment.voice.confidence)


def _word_count(body: str) -> int:
    """Return the whitespace-delimited token count of *body*."""
    return len(body.split())


def audience_authority(
    fragment: Fragment,
    weighting: VoiceAudienceWeightingConfig,
) -> float:
    """Return the audience-authority multiplier for *fragment*.

    Combines a per-``privacy_tier`` factor (public-facing work outranks
    private writing) with a per-``representativeness`` factor (the owner's own
    and endorsed material outranks aspirational and borrowed material). The
    result multiplies a fragment's ranking score so higher-authority fragments
    win ties and dominate the exemplars that feed each voice profile.

    A fragment whose file declared no ``privacy_tier`` reaches here as
    ``intimate`` — :func:`_load_fragment_with_body` materialises the declared
    tier (#1212) — and therefore scores ``0.0`` rather than ``unclassified``'s
    ``0.75``. That is deliberate and accepted: keyless content behaves exactly
    like declared-intimate content at every reader in this module, and on the
    ``allow_intimate=True`` path where it is still admitted a ``0.0`` only
    de-ranks it (:meth:`VoiceExemplarCollector.rank_exemplars` truncates by
    position, not by score). Do not carve an exception out here — a second
    tier opinion inside one module is the bug #1079 and #1212 both were.

    Args:
        fragment: The candidate exemplar fragment.
        weighting: The audience-weighting configuration. When disabled every
            fragment returns ``1.0`` (the pre-weighting uniform ranking).

    Returns:
        A non-negative multiplier; ``0.0`` for tiers with no authority
        (e.g. ``intimate``). Unknown tier / representativeness values fall
        back to ``1.0`` so a new enum member never silently zeroes a fragment.
    """
    if not weighting.enabled:
        return 1.0
    privacy = weighting.privacy_tier_authority.get(str(fragment.privacy_tier), 1.0)
    representativeness = weighting.representativeness_authority.get(
        str(fragment.representativeness),
        1.0,
    )
    return privacy * representativeness


def _is_other_authors_path(md_file: Path) -> bool:
    """Return whether *md_file* lives under an ``11-Other-Authors`` segment.

    Borrowed and AI-authored content is captured under
    ``11-Other-Authors/`` and must never seed the voice corpus (Issue
    #466). The check matches whole path *segments* (via ``Path.parts``)
    rather than a substring of the full path, so a folder merely
    *containing* the text — e.g. ``my-11-Other-Authors-notes`` — does not
    trigger a false exclusion.

    Args:
        md_file: Path to a candidate fragment markdown file.

    Returns:
        ``True`` when any path component equals the
        ``11-Other-Authors`` segment, otherwise ``False``.
    """
    return _OTHER_AUTHORS_SEGMENT in md_file.parts


def _is_safe_sample_stem(fragment_id: str) -> bool:
    """Return whether *fragment_id* may be used as a bare sample filename.

    :meth:`VoiceExemplarCollector._persist_fragment` composes
    ``<register_dir>/{fragment.id}.md``, and ``Fragment.id`` is an
    unconstrained ``str`` carried over from an export
    (``creek/models.py``), so an id has four separate hazards to clear
    before it can name a file:

    * **Traversal.** A separator or a ``..`` component steers the write
      out of the register folder entirely — ``id="../../escape"`` lands
      in ``07-Voice/``.
    * **Emptiness.** An empty id writes ``.md``, whose ``Path.stem`` is
      ``".md"`` rather than the id, so the prune pass could never
      recognise the file as its own again.
    * **Length.** The name that has to fit in a directory entry is
      ``f"{id}.md"``, three bytes more than the id, and the limit is
      :data:`_NAME_MAX_BYTES`. An over-long name reaches ``shutil.copy2``
      and raises ``OSError`` (``ENAMETOOLONG``), which takes the whole
      command down. The bound is measured on the **encoded** name because
      the kernel's is: ids arrive verbatim from exports, and a non-ASCII
      one costs more bytes than it shows characters.
    * **Collision with the summary.** ``_Summary`` would send a verbatim
      fragment body to the path :meth:`VoiceExemplarCollector._write_summary`
      is about to write. Today the summary wins by writing last; a process
      killed in between leaves a fragment body in ``_Summary.md`` instead
      — a file the prune exempts *by name* and so never cleans up.

    Same reasoning as the register guard in
    :meth:`VoiceProfileGenerator.write_profile`; the response differs
    because one unusable id is one unusable exemplar rather than a
    malformed call, so the collector skips it and goes on saving the rest.

    Windows device names (``CON``, ``NUL``), NTFS alternate data streams,
    and Unicode-normalisation collisions are deliberately **not** handled
    here — they are tracked in issue #1214, not overlooked.

    Args:
        fragment_id: The candidate id.

    Returns:
        ``True`` when ``f"{fragment_id}.md"`` is a plain filename that
        resolves inside its own directory and fits in a directory entry.
    """
    if fragment_id in _ID_DIRECTORY_NAMES or fragment_id == _SUMMARY_STEM:
        return False
    if any(char in fragment_id for char in _ID_PATH_CHARS):
        return False
    filename = f"{fragment_id}.md"
    if Path(filename).name != filename:
        return False
    return len(filename.encode("utf-8")) <= _NAME_MAX_BYTES


def _eligible_register(
    fragment: Fragment,
    *,
    allow_intimate: bool,
) -> str | None:
    """Return the fragment's register if it qualifies as an exemplar.

    Args:
        fragment: Candidate fragment.
        allow_intimate: When ``False``, ``intimate`` privacy tier
            fragments are rejected.

    Returns:
        The canonical register string when the fragment is self-authored
        and has qualifying confidence, a positive ``voice_weight``, allowed
        privacy tier, and a canonical register; otherwise ``None``.
    """
    # Authorship gate (Issue #1213), first because it is the primary one:
    # this is what enforces the guarantee
    # :attr:`~creek.models.Fragment.voice_proxy_eligible` documents — that
    # AI-, collaborator- and other-authored prose can never train the voice
    # proxy. Before this clause the corpus consulted no authorship axis at
    # all, and the two gates below were the whole defence.
    #
    # Re-derived rather than delegated to the property, exactly as
    # :func:`creek.generate.skills._is_snapshot_fragment` is and for the
    # same documented reason: the property bakes in the INTIMATE exclusion,
    # while ``allow_intimate=True`` deliberately overrides it, so
    # ``return fragment.voice_proxy_eligible`` would silently revoke the
    # operator's opt-in to their *own* intimate writing.
    #
    # Compared by value, not identity: ``FragmentSource`` sets
    # ``use_enum_values=True``, so ``author`` is a plain ``str`` at runtime
    # — the same idiom as the privacy-tier comparison below. Deliberately
    # not ``Authorship(...) is Authorship.SELF``, which raises on a
    # hand-corrupted frontmatter value instead of failing closed.
    if str(fragment.source.author) != Authorship.SELF.value:
        return None
    # Voice-fidelity backstop (Issue #466): ``ai-as-user`` content is
    # stamped ``voice_weight=0.0`` at ingest, which refuses it a second
    # time even if its authorship axis is ever mis-set. ``voice_weight`` is
    # already coerced to a float in ``[0, 1]`` by the model, so a plain
    # comparison is safe.
    if fragment.voice_weight <= 0:
        return None
    if _confidence_value(fragment) not in _QUALIFYING_CONFIDENCE:
        return None
    if not allow_intimate and str(fragment.privacy_tier) == PrivacyTier.INTIMATE.value:
        return None
    register = _register_value(fragment)
    if register not in VOICE_REGISTERS:
        return None
    return register


class _JsonlWriter:
    """Append-only JSONL writer used by the streaming exemplar accumulator.

    Holds the file handle open for the duration of the walk so each
    appended line costs an ``fwrite`` rather than an ``open + close``,
    and flushes/closes the handle when the caller invokes
    :meth:`close` (or the surrounding context manager exits).
    """

    def __init__(self, path: Path) -> None:
        """Open *path* for append-binary writing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Binary mode keeps newlines pristine across platforms; we do
        # the encoding ourselves so a stray non-UTF8 char surfaces
        # immediately rather than silently coercing.
        self._fp = path.open("ab")

    def append(self, payload: dict[str, object]) -> None:
        """Serialise *payload* as a single JSON line and append it.

        Each line is flushed to the OS so an interrupted run (SIGKILL,
        OOM) leaves a coherent prefix rather than half a line. The
        flush is per-line; the kernel still buffers to disk, so this
        is a debuggability gain rather than a durability guarantee.
        """
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._fp.write(line)
        self._fp.flush()

    def close(self) -> None:
        """Flush and close the underlying file handle."""
        if not self._fp.closed:
            self._fp.flush()
            self._fp.close()


def _iter_exemplars_from_jsonl(jsonl_path: Path) -> Iterator[Exemplar]:
    """Yield :class:`Exemplar` objects from a streaming-accumulator JSONL file.

    Lines that fail to parse are skipped with a debug log entry so a
    partial flush under interrupt does not poison the rest of the
    register's analysis.
    """
    with jsonl_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                logger.debug("Skipping malformed exemplar line in %s", jsonl_path)
                continue
            if not isinstance(payload, dict):
                continue
            fragment_data = payload.get("fragment")
            body = payload.get("body")
            if not isinstance(fragment_data, dict) or not isinstance(body, str):
                continue
            try:
                fragment = Fragment.model_validate(fragment_data)
            except ValidationError:
                logger.debug("Skipping invalid fragment payload in %s", jsonl_path)
                continue
            yield Exemplar(fragment=fragment, body=body)


def _load_fragment_with_body(
    md_file: Path,
) -> tuple[Fragment, str, dict[str, object]] | None:
    """Parse *md_file* into a Fragment and return it with body and raw frontmatter.

    The returned model carries the **declared** tier: this loader overwrites
    ``privacy_tier`` with
    :func:`~creek.classify.privacy_filter.raw_privacy_tier` of the raw
    frontmatter, so a file with no ``privacy_tier`` key resolves to
    ``INTIMATE`` (``creek/classify/privacy_filter.py:409-415``) instead of
    reaching the consent gate as Pydantic's ``unclassified`` default (#1212).
    That doctrine is not new here — "the tier field is missing → ``intimate``"
    is already published to every vault at
    ``creek/templates/skills/privacy-tier.SKILL.md:27`` — and the shape is
    copied verbatim from :func:`creek.generate.mining._admitted_liminal_entry`
    (``creek/generate/mining.py:441``), which closed the identical hole in
    #1079. Doing it once, at the single loader every walk in this module goes
    through, is what makes all three walks see one tier per file.

    The substitution is **one-way**: it can only ever refuse more. Whenever the
    key is present the two readers agree — both read the same dict, and an
    unparseable value already failed ``model_validate`` above — so the only
    fragment whose tier moves is the keyless one, and it moves towards
    ``intimate``.

    The raw frontmatter is still returned alongside the model because the
    ceiling gate (#968) runs *before* the consent gate at each walk and takes
    raw: :func:`~creek.classify.privacy_filter.within_ceiling` must keep
    reading frontmatter rather than a model, since the tier it needs precedes
    validation.

    One subtlety the callers depend on: ``Fragment``'s config is
    ``use_enum_values=True`` (``creek/models.py:766``), so ``privacy_tier`` is
    normally a plain ``str`` at runtime, and ``model_copy(update=...)``
    bypasses validation — after this substitution that one field holds a
    genuine :class:`~creek.models.PrivacyTier` member. Every reader survives
    because ``PrivacyTier`` is a ``StrEnum`` (``creek/models.py:333``):
    ``str(member)`` is the value, ``model_dump(mode="json")`` emits the value,
    and ``!=`` against another tier compares by value.

    Args:
        md_file: Markdown file to read.

    Returns:
        A ``(fragment, body, raw)`` triple whose fragment carries the declared
        tier, or ``None`` when the file is not a valid fragment record
        (unreadable, wrong type, or invalid frontmatter).
    """
    try:
        post = frontmatter.load(str(md_file))
    except FRONTMATTER_LOAD_ERRORS:
        logger.debug("Skipping unreadable markdown file: %s", md_file)
        return None
    metadata = post.metadata
    if metadata.get("type") != "fragment":
        return None
    try:  # noqa: TRY101  # Separate failure modes: file IO vs schema validation each return None with distinct debug logs.
        fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    # Materialised unconditionally: for a file that reaches this line the key
    # is either absent (the one case that changes) or a valid tier string that
    # ``raw_privacy_tier`` reads back identically off the same dict. A guard
    # would only add an arm that can never be taken.
    declared = fragment.model_copy(
        update={"privacy_tier": raw_privacy_tier(metadata)},
    )
    return declared, post.content, metadata


def _sample_digest(stem: str) -> str:
    """Return the manifest fingerprint of a sample filename *stem*.

    The manifest records fingerprints rather than the ids themselves
    because of where it lives — the frontmatter of ``_Summary.md``, an
    ordinary ``.md`` file inside the vault. ``creek purge`` rewrites
    **every** ``.md`` in the vault in place, replacing a purged id with
    ``[purged]`` (``creek/purge/engine.py``). A plaintext manifest is
    therefore edited out from under the prune by the very command whose
    leftovers the prune exists to remove, leaving the copy undeletable. A
    digest survives that rewrite untouched, so the copy stays deletable.

    **This is a stability property, not an erasure guarantee.** The digest
    is unsalted SHA-256 over a low-entropy, enumerable value: fragment ids
    come verbatim from exports, so anyone holding a candidate id list can
    confirm membership by recomputing the digest, including after a purge.
    The manifest is therefore best understood as a *confirmable* record of
    which ids this folder once held, not as an anonymised one. It is
    accepted because the alternative — a plaintext ledger somewhere purge
    cannot reach — is strictly worse residue, and because the prune it
    enables removes the fragment *bodies*, which are the sensitive part.
    Domain-separating the digest (HMAC under a per-vault key) would close
    the confirmation gap and is the obvious upgrade if that judgement ever
    changes.

    Args:
        stem: The sample filename stem — equivalently the fragment id,
            since :meth:`VoiceExemplarCollector._persist_fragment` writes
            ``f"{fragment.id}.md"``.

    Returns:
        The hex digest recorded for *stem*.
    """
    return hashlib.sha256(stem.encode("utf-8")).hexdigest()


def _read_sample_manifest(register_dir: Path) -> frozenset[str]:
    """Return the digests the previous run recorded writing into *register_dir*.

    A missing, unreadable, or manifest-less summary yields an empty set,
    which makes the prune a no-op. That is the safe direction: the
    collector deletes only what it can prove it wrote, so a lost record
    costs a stale copy rather than an operator's file.

    The header is read with :func:`~creek.vault.links.read_header_meta`, which
    folds every unreadable shape into that empty mapping — and, unlike
    ``frontmatter.load``, never splats the parsed header into keyword
    arguments, so a non-string frontmatter key no longer raises ``TypeError``
    out of a prune (#1475). Only :data:`_MANIFEST_KEY` is read; the summary's
    body is not consulted.

    Header-only reading carries the same three deliberate consequences #1416
    accepted and documents in full at
    :func:`creek.generate.synchronicity._existing_synchronicity_pairs`: the
    ``---`` fence must open line 1, the 200-line / 64 KB header caps apply, and
    a note carrying a stray non-string key is tolerated rather than rejected.

    Args:
        register_dir: The register's samples folder, which may not exist.

    Returns:
        Every entry recorded under :data:`_MANIFEST_KEY`, read as text —
        the note is on disk and may have been hand-edited, and an entry
        that is not a digest simply matches no file.
    """
    summary_path = register_dir / _SUMMARY_FILENAME
    if not summary_path.is_file():
        return frozenset()
    recorded = read_header_meta(summary_path).get(_MANIFEST_KEY)
    if not isinstance(recorded, list):
        return frozenset()
    return frozenset(str(entry) for entry in recorded)


class VoiceExemplarCollector:
    """Collect, rank, and persist voice exemplars from the vault.

    The collector caches per-fragment word counts and source paths during
    :meth:`collect_exemplars` so that :meth:`rank_exemplars` and
    :meth:`save_exemplars` can reuse the data without re-reading disk.
    Calling ``rank_exemplars`` or ``save_exemplars`` before
    ``collect_exemplars`` still works — fragments without cached data
    fall back to in-memory serialisation.

    Attributes:
        max_per_register: Maximum exemplars retained per register after
            ranking.
        min_per_register: Minimum exemplars expected per register; below
            this threshold the collector emits a warning.
        allow_intimate: When ``True``, fragments tagged ``intimate``
            participate in collection. Defaults to ``False``. A *consent*
            gate, not a ceiling one — see :attr:`override`.
        override: The caller's tier ceiling (#968). Additive to
            :attr:`allow_intimate`: a fragment is collected only when both
            admit it.
    """

    def __init__(
        self,
        *,
        max_per_register: int = DEFAULT_MAX_PER_REGISTER,
        min_per_register: int = DEFAULT_MIN_PER_REGISTER,
        allow_intimate: bool = False,
        override: PrivacyTierOverride = PrivacyTierOverride.ALL,
        audience_weighting: VoiceAudienceWeightingConfig | None = None,
    ) -> None:
        """Initialise the collector.

        Args:
            max_per_register: Cap on exemplars retained per register.
                Must be at least 1.
            min_per_register: Minimum exemplars per register before a
                warning is emitted. Must be at least 1.
            allow_intimate: Whether to include ``intimate`` privacy tier
                fragments. Defaults to ``False``. This is the FEAT opt-in
                for the voice proxy, which
                ``creek/templates/skills/privacy-tier.SKILL.md:47`` requires
                "even with ``--include-tier intimate``" — so it is *not*
                superseded by *override* and the two must stay separate.
            override: Tier ceiling for the corpus walk (#968). Defaults to
                :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
                meaning "no ceiling declared", so adding it is a genuine
                no-op for every caller that predates #968. The default is
                safe because both production report surfaces state an
                override explicitly, which
                ``tests/test_mcp_report_tier_ceiling.py``'s
                ``test_production_report_callers_always_state_an_override``
                enforces structurally.
            audience_weighting: Graduated audience-authority multipliers
                applied during ranking. Defaults to the standard weighting,
                which keeps a uniform-audience vault's relative ranking
                unchanged while letting public-facing work dominate a
                mixed-audience one. On **this** path a ``0.0`` authority
                only de-ranks: :meth:`rank_exemplars` returns
                ``scored[:max_per_register]`` whatever the scores are, so the
                weighting can never admit or exclude a fragment — membership
                belongs to :func:`within_ceiling` and :meth:`_eligible_register`
                alone. (The fingerprint path differs: ``if weight > 0.0`` at
                ``creek/generate/ai_style/fingerprint.py`` makes a zero
                authority a genuine membership gate there. See #1313.)

        Raises:
            ValueError: If ``max_per_register`` or ``min_per_register``
                is less than 1, or if ``min_per_register`` exceeds
                ``max_per_register``.
        """
        if max_per_register < 1:
            msg = f"max_per_register must be >= 1, got {max_per_register}"
            raise ValueError(msg)
        if min_per_register < 1:
            msg = f"min_per_register must be >= 1, got {min_per_register}"
            raise ValueError(msg)
        if min_per_register > max_per_register:
            msg = (
                f"min_per_register ({min_per_register}) "
                f"> max_per_register ({max_per_register})"
            )
            raise ValueError(msg)
        self.max_per_register = max_per_register
        self.min_per_register = min_per_register
        self.allow_intimate = allow_intimate
        self.override = override
        self.audience_weighting = audience_weighting or VoiceAudienceWeightingConfig()
        self._records: dict[str, _ExemplarRecord] = {}

    # ---- Collection ----

    def collect_exemplars(self, vault_path: Path) -> dict[str, list[Fragment]]:
        """Scan ``01-Fragments/`` and group qualifying exemplars by register.

        Fragments are kept when they are **self-authored** (#1213), their
        ``voice.confidence`` is ``settled`` or ``conviction``, their
        ``voice_weight`` is positive, and their ``voice.register`` is set.
        Intimate privacy tier fragments are filtered out unless
        :attr:`allow_intimate` is ``True``, and so are untiered ones — a file
        with no ``privacy_tier`` key reads as ``intimate`` at every gate
        (#1212; see :func:`_load_fragment_with_body`). Every fragment must
        additionally sit within :attr:`override` (#968). Each register key
        in the returned dict is always present; empty registers map to
        ``[]``.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Dict mapping every voice register to its qualifying
            fragments, in the order they were discovered on disk.
        """
        self._records = {}
        buckets: dict[str, list[Fragment]] = {reg: [] for reg in VOICE_REGISTERS}
        fragments_dir = vault_path / _FRAGMENTS_SUBDIR
        if not fragments_dir.is_dir():
            self._warn_below_minimum(buckets)
            return buckets

        for md_file in sorted(fragments_dir.rglob("*.md")):
            # Defensive path gate (Issue #466): skip borrowed / AI-authored
            # content under ``11-Other-Authors`` before parsing, so it never
            # reaches the voice corpus even if symlinked or the scan root
            # later broadens.
            if _is_other_authors_path(md_file):
                continue
            loaded = _load_fragment_with_body(md_file)
            if loaded is None:
                continue
            fragment, body, raw = loaded
            # Ceiling gate (#968), above the consent gate: an above-ceiling
            # fragment must not reach the corpus even when the operator has
            # opted the voice proxy in to intimate content.
            if not within_ceiling(raw, self.override):
                continue
            register = self._eligible_register(fragment)
            if register is None:
                continue
            buckets[register].append(fragment)
            self._records[fragment.id] = _ExemplarRecord(
                word_count=_word_count(body),
                source_path=md_file,
            )

        self._warn_below_minimum(buckets)
        return buckets

    def collect_all_exemplars(self, vault_path: Path) -> list[Exemplar]:
        """Collect every qualifying exemplar across all registers, with bodies.

        Shares the eligibility gate with :meth:`collect_exemplars`
        (self-authored, ``settled`` or ``conviction`` confidence, positive
        ``voice_weight``, a set register, intimate — and, since #1212,
        untiered — filtered unless :attr:`allow_intimate`, within
        :attr:`override`, ``11-Other-Authors`` excluded) but returns a flat
        :class:`Exemplar` list — fragment paired
        with its on-disk body — rather than per-register ``Fragment`` buckets.
        Consumers that need the whole voice corpus as one unit (e.g. the
        lexicon) use this so they share a single source of truth for *which*
        fragments count as exemplars.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Every qualifying exemplar with its body, in on-disk discovery order.
        """
        fragments_dir = vault_path / _FRAGMENTS_SUBDIR
        exemplars: list[Exemplar] = []
        if not fragments_dir.is_dir():
            return exemplars
        for md_file in sorted(fragments_dir.rglob("*.md")):
            if _is_other_authors_path(md_file):
                continue
            loaded = _load_fragment_with_body(md_file)
            if loaded is None:
                continue
            fragment, body, raw = loaded
            # Ceiling gate (#968) — see collect_exemplars for why it sits
            # above the consent gate rather than folded into it.
            if not within_ceiling(raw, self.override):
                continue
            if self._eligible_register(fragment) is None:
                continue
            exemplars.append(Exemplar(fragment=fragment, body=body))
        return exemplars

    def _eligible_register(self, fragment: Fragment) -> str | None:
        """Return the fragment's register if it qualifies, else ``None``.

        This answers the *consent* question only (``allow_intimate``, plus
        authorship / confidence / voice-weight / register eligibility).
        The *ceiling*
        question is asked separately by
        :func:`~creek.classify.privacy_filter.within_ceiling` at each walk,
        because it needs the raw frontmatter this method never sees. Do not
        collapse the two: one is the operator's opt-in to voice-proxy
        training, the other is the caller's declared ceiling, and a fragment
        needs both.

        The tier this method reads off the model *is* the declared one, even
        though it never sees frontmatter, because
        :func:`_load_fragment_with_body` materialised it there first (#1212).
        The model still cannot distinguish "no key on disk" from an explicit
        ``unclassified`` — it does not have to: the loader has already
        resolved the keyless case to ``intimate``, and the model value
        ``UNCLASSIFIED`` keeps its #876 standing here.
        """
        return _eligible_register(fragment, allow_intimate=self.allow_intimate)

    def _warn_below_minimum(self, buckets: dict[str, list[Fragment]]) -> None:
        """Emit a warning for registers with some but too few exemplars.

        Registers with zero exemplars are silently skipped — a warning
        is only useful when a register *has* data but not enough.
        """
        for register, frags in buckets.items():
            if 0 < len(frags) < self.min_per_register:
                logger.warning(
                    "Voice register %r has only %d exemplars (minimum %d).",
                    register,
                    len(frags),
                    self.min_per_register,
                )

    # ---- Ranking ----

    def rank_exemplars(self, fragments: list[Fragment]) -> list[Fragment]:
        """Rank *fragments* by quality and return the top ``max_per_register``.

        Quality base (max 5 per fragment):

        - ``conviction`` confidence: 3 points; ``settled``: 2 points.
        - Body length in ``[200, 800]`` words: +1 point (uses the cached
          word count from :meth:`collect_exemplars`; falls back to 0
          when the fragment was not previously collected).
        - All classification axes populated (frequency, wavelength
          phase / mode, voice register, voice confidence): +1 point.

        The base is then multiplied by :func:`audience_authority`, so a
        public-facing (``OPEN``) fragment outranks an otherwise-equal
        ``PERSONAL`` one. Ties are broken by ascending ID for deterministic
        ordering.

        Args:
            fragments: Fragments to rank. May be empty.

        Returns:
            Up to :attr:`max_per_register` fragments, sorted by
            descending score.
        """
        if not fragments:
            return []
        scored = sorted(
            fragments,
            key=lambda f: (-self._score(f), f.id),
        )
        return scored[: self.max_per_register]

    def _score(self, fragment: Fragment) -> float:
        """Return the audience-weighted ranking score for *fragment*.

        The quality base (confidence + length + classification bonuses) is
        multiplied by :func:`audience_authority`, so public-facing,
        self-representative fragments win ties and outrank private ones.
        """
        score = _CONFIDENCE_SCORE.get(_confidence_value(fragment), 0)
        record = self._records.get(fragment.id)
        if record is not None and (
            _MEDIUM_LENGTH_MIN_WORDS <= record.word_count <= _MEDIUM_LENGTH_MAX_WORDS
        ):
            score += _LENGTH_BONUS
        if _is_fully_classified(fragment):
            score += _CLASSIFICATION_BONUS
        return score * audience_authority(fragment, self.audience_weighting)

    # ---- Persistence ----

    def save_exemplars(
        self,
        exemplars: dict[str, list[Fragment]],
        vault_path: Path,
        *,
        on_prune: Callable[[Path], None] | None = None,
    ) -> dict[str, Path]:
        """Copy ranked exemplars into ``07-Voice/Register-Samples/<register>/``.

        For every register named in *exemplars* the collector first prunes
        the copies an earlier run recorded writing (#879), then — for the
        non-empty ones — creates the destination folder, copies (or
        rewrites) up to :attr:`max_per_register` of the top-ranked
        fragments, and writes a ``_Summary.md`` note recording statistics
        about the cohort. A register that has just emptied keeps neither:
        its existing summary is rewritten to an honest zero rather than
        left describing the cohort that has gone.

        Fragments in each register bucket are ranked internally via
        :meth:`rank_exemplars` before being persisted — callers do not
        need to pre-rank.

        Args:
            exemplars: Mapping of register → fragments, typically the
                output of :meth:`collect_exemplars`. Non-canonical
                register keys are skipped with a debug log entry, and are
                not pruned either: an unrecognised key describes a folder
                this collector never wrote.
            vault_path: Path to the root of the Obsidian vault.
            on_prune: Invoked once per deleted copy, with its path.
                Deletion is the half of this pass the return value cannot
                describe, and ``creek fill`` runs it unattended, so the
                CLI passes a collector here to report it (#879).

        Returns:
            Mapping of register → path of the written summary note, one
            entry per register that had exemplars to rank. A register that
            has just lost its last exemplar gets its existing summary
            rewritten to an honest zero, but is deliberately **absent**
            from the mapping: callers count these entries as samples
            written, and that register had none.
        """
        samples_root = vault_path.joinpath(*_SAMPLES_SUBPATH)
        summaries: dict[str, Path] = {}
        for register, fragments in exemplars.items():
            if register not in VOICE_REGISTERS:
                logger.debug("Skipping unknown voice register %r", register)
                continue
            summary = self._save_register(
                register,
                fragments,
                samples_root / register,
                on_prune,
            )
            if summary is not None:
                summaries[register] = summary
        return summaries

    def _save_register(
        self,
        register: str,
        fragments: list[Fragment],
        register_dir: Path,
        on_prune: Callable[[Path], None] | None,
    ) -> Path | None:
        """Prune, persist, and summarise a single register.

        Args:
            register: The canonical register being saved.
            fragments: Every candidate fragment collected for it.
            register_dir: Its samples folder, which may not exist yet.
            on_prune: Per-deletion callback, forwarded to
                :meth:`_prune_stale_copies`.

        Returns:
            Path of the summary note describing this run's cohort, or
            ``None`` when the register had nothing to rank.
        """
        ranked = self.rank_exemplars(fragments)
        # Above the empty-bucket branch on purpose: a register that lost
        # every exemplar is exactly the case with the most to clear.
        self._prune_stale_copies(register_dir, {f.id for f in ranked}, on_prune)
        if not ranked:
            self._rewrite_emptied_summary(register, register_dir)
            return None
        register_dir.mkdir(parents=True, exist_ok=True)
        # Only the fragments that actually reached disk are summarised: an
        # id the filename guard rejected must not be counted, wikilinked,
        # or recorded in the manifest, because a manifest entry for a file
        # that was never written aims the next run's prune at a phantom.
        persisted = [
            fragment
            for fragment in ranked
            if self._persist_fragment(fragment, register_dir) is not None
        ]
        return self._write_summary(register, persisted, register_dir)

    def _rewrite_emptied_summary(self, register: str, register_dir: Path) -> None:
        """Rewrite the summary of a register that has just lost every exemplar.

        The prune removes the copies, but the summary an earlier run wrote
        outlives them and goes on naming the departed fragments by id
        **and title**, and goes on reporting the ``tier_ceiling`` of a
        corpus that no longer exists. That residue is introduced by the
        prune pass itself, so it is the prune pass that has to clear it
        (#879).

        Nothing is created here. A register that never had a summary keeps
        no folder and gains no note, which is what
        ``tests/test_voice_exemplars.py::TestSaveExemplars::
        test_skips_empty_registers`` holds.

        Args:
            register: The canonical register that emptied.
            register_dir: Its samples folder, which may not exist.
        """
        if not (register_dir / _SUMMARY_FILENAME).is_file():
            return
        self._write_summary(register, [], register_dir)

    @staticmethod
    def _prune_stale_copies(
        register_dir: Path,
        keep_ids: set[str],
        on_prune: Callable[[Path], None] | None,
    ) -> None:
        """Delete exemplar copies from an earlier run that no longer rank.

        Persisting alone only ever adds, so without this pass a fragment
        that drops out of the top :attr:`max_per_register`, is re-tiered
        above the caller's ceiling, or is deleted from the vault outright
        leaves a verbatim copy of its body in the samples tree forever
        (#879).

        A file is deleted only when the *previous* summary's manifest
        records this collector writing it and this run is not about to
        rewrite it. Authorship is a record, never a guess:
        ``Register-Samples/<register>/`` is a folder an operator keeps
        their own notes in, and a fragment they curated into it is — in
        shape, byte for byte — indistinguishable from one this tool wrote.
        Conversely the match is on the filename **stem**, so scrubbing a
        copy's frontmatter, which ``creek purge`` does to every ``.md`` in
        the vault, cannot make it unrecognisable and therefore immortal.

        The scan is deliberately non-recursive, and never creates
        *register_dir*, so a register with no exemplars stays folderless.

        Args:
            register_dir: The register's samples folder. Left untouched
                when it does not exist.
            keep_ids: Ids of the exemplars this run is about to write.
            on_prune: Invoked with the path of each deleted copy, or
                ``None`` when the caller does not need to know.
        """
        manifest = _read_sample_manifest(register_dir)
        if not manifest:
            return
        for candidate in sorted(register_dir.glob("*.md")):
            # The summary is exempted by name as well as by manifest: it
            # is the one file in here the collector rewrites rather than
            # replaces, and losing it would lose the manifest with it.
            if candidate.name == _SUMMARY_FILENAME or candidate.stem in keep_ids:
                continue
            if _sample_digest(candidate.stem) not in manifest:
                continue
            candidate.unlink()
            # Named, not just counted. The CLI reports a total, but a total
            # scrolls past inside an unattended ``creek fill`` and cannot
            # answer "which file did it take?". WARNING is the level rather
            # than INFO because creek configures no logging handlers, so
            # INFO is swallowed by ``logging.lastResort`` and a silent
            # deletion is one nobody can audit (#879).
            logger.warning("Pruned stale voice register sample: %s", candidate)
            if on_prune is not None:
                on_prune(candidate)

    def _persist_fragment(self, fragment: Fragment, register_dir: Path) -> Path | None:
        """Copy the source file for *fragment* (or serialise from memory).

        Args:
            fragment: The ranked exemplar to persist.
            register_dir: The register's samples folder, already created.

        Returns:
            Path of the written copy, or ``None`` when ``fragment.id`` is
            unusable as a filename (see :func:`_is_safe_sample_stem`) and
            nothing was written.
        """
        if not _is_safe_sample_stem(fragment.id):
            logger.warning(
                "Skipping voice exemplar %r: its id is not usable as a filename.",
                fragment.id,
            )
            return None
        target = register_dir / f"{fragment.id}.md"
        record = self._records.get(fragment.id)
        if record is not None and record.source_path.exists():
            shutil.copy2(record.source_path, target)
            return target
        data = fragment.model_dump(mode="json")
        post = frontmatter.Post(content="", **data)
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target

    def _write_summary(
        self,
        register: str,
        persisted: list[Fragment],
        register_dir: Path,
    ) -> Path:
        """Write the per-register summary note and return its path.

        Beyond its human-readable statistics the note carries two records.

        The :attr:`override` the run was taken under, for the same reason
        ``00-Creek-Meta/Processing-Log/tag-history.json`` does
        (``creek_mcp/tools/report.py``): a narrow ceiling surveys a
        smaller corpus *and* prunes samples a wider one produced, so two
        summaries written at two ceilings describe two different corpora
        and their counts are not comparable.

        And the manifest under :data:`_MANIFEST_KEY`, which is the record
        of which copies in this folder are the collector's own and the
        only thing that entitles the next run to delete one. It
        fingerprints the fragments that actually reached disk — see
        :func:`_sample_digest` for why the ids themselves are not stored.

        Args:
            register: The canonical register this cohort belongs to.
            persisted: The exemplars written alongside this note. Empty
                when the register has just lost its last one.
            register_dir: The register's samples folder.

        Returns:
            Path of the written summary note.
        """
        conviction = sum(
            1 for f in persisted if _confidence_value(f) == Confidence.CONVICTION.value
        )
        settled = sum(
            1 for f in persisted if _confidence_value(f) == Confidence.SETTLED.value
        )
        body = self._render_summary_body(register, persisted, conviction, settled)
        post = frontmatter.Post(
            content=body,
            type="voice-register-summary",
            voice_register=register,
            exemplar_count=len(persisted),
            conviction_count=conviction,
            settled_count=settled,
            tier_ceiling=self.override.value,
            generated_at=datetime.now(tz=UTC).isoformat(),
            tags=["voice", "voice-register", register],
        )
        # Assigned rather than passed as a keyword so the key stays the
        # single constant the prune's reader looks it up by.
        post.metadata[_MANIFEST_KEY] = [_sample_digest(f.id) for f in persisted]
        summary_path = register_dir / _SUMMARY_FILENAME
        summary_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return summary_path

    @staticmethod
    def _render_summary_body(
        register: str,
        persisted: list[Fragment],
        conviction: int,
        settled: int,
    ) -> str:
        """Render the markdown body of a register summary note."""
        lines: list[str] = [
            f"# Voice Register: {register}",
            "",
            "## Statistics",
            "",
            f"- Exemplar count: {len(persisted)}",
            f"- Conviction confidence: {conviction}",
            f"- Settled confidence: {settled}",
            "",
            "## Exemplars",
            "",
        ]
        if persisted:
            for fragment in persisted:
                lines.append(f"- [[{fragment.id}|{fragment.title}]]")
        else:
            lines.append("_No exemplars collected._")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Voice pattern extraction — data models & extractor
# ---------------------------------------------------------------------------

METAPHOR_DOMAINS: dict[str, tuple[str, ...]] = {
    "water": (
        "creek",
        "flow",
        "pool",
        "eddy",
        "current",
        "stream",
        "wave",
        "tide",
        "ripple",
        "flood",
        "drown",
        "shore",
    ),
    "light": (
        "illuminate",
        "shadow",
        "dark",
        "bright",
        "glow",
        "shine",
        "radiant",
        "dim",
        "light",
        "luminous",
    ),
    "fire": (
        "burn",
        "flame",
        "ignite",
        "spark",
        "blaze",
        "smolder",
        "kindle",
        "ash",
        "ember",
    ),
    "body": (
        "heart",
        "gut",
        "bone",
        "muscle",
        "breath",
        "blood",
        "skin",
        "nerve",
        "spine",
    ),
}
"""Keyword lists per metaphor domain used by :class:`VoicePatternExtractor`."""

_TRANSITION_WORDS: tuple[str, ...] = (
    "however",
    "therefore",
    "moreover",
    "furthermore",
    "nevertheless",
    "consequently",
    "meanwhile",
    "similarly",
    "additionally",
    "alternatively",
    "instead",
    "otherwise",
    "nonetheless",
    "likewise",
    "accordingly",
    "hence",
    "thus",
    "besides",
)

_TRANSITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (word, re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE))
    for word in _TRANSITION_WORDS
)
"""Pre-compiled case-insensitive regexes for each transition word."""

_SELF_DEPRECATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bi'?m\s+(?:probably|not\s+sure)", re.IGNORECASE),
    re.compile(r"\bthis\s+is\s+probably\s+not", re.IGNORECASE),
    re.compile(r"\bthis\s+might\s+be\s+silly", re.IGNORECASE),
    re.compile(r"\bmaybe\s+i'?m\s+overthinking", re.IGNORECASE),
)

_PARADOX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\band\s+yet\b", re.IGNORECASE),
    re.compile(r"\bbut\s+also\b", re.IGNORECASE),
    re.compile(r"\bat\s+the\s+same\s+time\b", re.IGNORECASE),
    re.compile(r"\bparadox\b", re.IGNORECASE),
)

_CALLBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bas\s+i\s+mentioned", re.IGNORECASE),
    re.compile(r"\bgoing\s+back\s+to\b", re.IGNORECASE),
    re.compile(r"\breturning\s+to\b", re.IGNORECASE),
    re.compile(r"\bas\s+we\s+discussed\b", re.IGNORECASE),
    re.compile(r"\bearlier\s+i\s+said\b", re.IGNORECASE),
    re.compile(r"\brecall\s+that\b", re.IGNORECASE),
)

_SENTENCE_SPLIT_RE: re.Pattern[str] = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\"'\u201c])",
)
"""Split text into sentences on terminal punctuation followed by a capital.

NOTE: Known limitation — abbreviations followed by a capitalised word
(e.g. ``"Mr. Smith"``, ``"U.S. Senator"``) are split as if the period
ended the sentence, slightly inflating ``total_sentences``. Acceptable
for informal personal-journal prose; callers analysing formal or
academic writing may want a dedicated tokenizer.
"""

_EM_DASH_RE: re.Pattern[str] = re.compile(r"\u2014|--")
_ELLIPSIS_RE: re.Pattern[str] = re.compile(r"\.{3}|\u2026")
_PARENTHETICAL_RE: re.Pattern[str] = re.compile(r"\([^)]+\)")
_EXCLAMATION_RE: re.Pattern[str] = re.compile(r"!")

# Citation / quotation / reference signals. Each matched span is one
# "citation signal"; their per-1000-word rate is ``citation_density``.
_QUOTATION_SPAN_RE: re.Pattern[str] = re.compile(r'"[^"\n]+"|\u201c[^\u201d\n]+\u201d')
_BLOCKQUOTE_LINE_RE: re.Pattern[str] = re.compile(r"^\s*>", re.MULTILINE)
_ATTRIBUTION_RE: re.Pattern[str] = re.compile(
    r"\baccording to\b"
    r"|\bas [^,.\n]{1,40}? (?:notes|writes|argues|observes|put it|puts it|said|says)\b"
    r"|\bin [^,.\n]{1,30}?'s words\b",
    re.IGNORECASE,
)
_REFERENCE_MARKER_RE: re.Pattern[str] = re.compile(
    r"\[\d+\]|\([A-Z][A-Za-z.-]+(?: et al\.?)?,?\s+\d{4}[a-z]?\)",
)
# Stop before terminal punctuation so a trailing ``.``/``,`` does not inflate
# the match (e.g. "see https://example.com." counts one clean link).
_OUTBOUND_LINK_RE: re.Pattern[str] = re.compile(r"https?://[^\s.,;:!?)]+")

_MIN_QUOTE_WORDS: int = 4
"""Minimum whitespace-token count for a quoted span to count as a citation.

Quoting a *source* tends to span several words; short quoted spans are almost
always dialogue tags, scare quotes (``the "obvious" fix``), or inline code
(``"--verbose"``), so requiring at least this many words filters those false
positives out of ``citation_density``."""

_CITATION_PROMPT_THRESHOLD: float = 1.0
"""Citation density (signals per 1000 words) above which the generated voice
skill explicitly instructs the writer to reference and quote sources."""

_SHORT_SENTENCE_MAX: int = 9
"""Sentences with at most this many words are classified as *short*."""

_LONG_SENTENCE_MIN: int = 26
"""Sentences with at least this many words are classified as *long*."""

_FRAGMENT_MAX_WORDS: int = 3
"""Sentences with at most this many words are counted as *fragments*."""

_PER_THOUSAND: float = 1000.0
"""Multiplier for per-1000-word frequency calculations."""

_DEFAULT_TOP_DISTINCTIVE: int = 20
"""Default number of distinctive words returned by vocabulary analysis."""

_MIN_PHRASE_OCCURRENCES: int = 2
"""Minimum occurrences for a bigram to count as a recurring phrase."""


@dataclass(frozen=True)
class SentenceMetrics:
    """Sentence structure metrics extracted from voice exemplar texts.

    Attributes:
        average_length: Mean word count per sentence.
        short_count: Sentences with at most 9 words.
        medium_count: Sentences with 10-25 words.
        long_count: Sentences with 26 or more words.
        fragment_count: Very short sentences (≤3 words) used as
            rhetorical fragments.
        question_frequency: Fraction of sentences ending with ``?``.
        total_sentences: Total number of sentences analysed.
    """

    average_length: float
    short_count: int
    medium_count: int
    long_count: int
    fragment_count: int
    question_frequency: float
    total_sentences: int


@dataclass(frozen=True)
class ParagraphMetrics:
    """Paragraph structure metrics.

    Attributes:
        average_sentences_per_paragraph: Mean sentence count across
            all paragraphs in the analysed texts.
    """

    average_sentences_per_paragraph: float


@dataclass(frozen=True)
class MetaphorFamily:
    """A detected metaphor domain with matched keywords and examples.

    Attributes:
        domain: The metaphor domain name (e.g. ``"water"``).
        matches: Keywords from the domain that were found in the text.
        example_sentences: Representative sentences containing matches.
    """

    domain: str
    matches: tuple[str, ...]
    example_sentences: tuple[str, ...]


@dataclass(frozen=True)
class RhetoricalMoves:
    """Counts of detected rhetorical move patterns.

    Attributes:
        self_deprecation_count: Instances of self-deprecating hedges
            before an insight.
        paradox_count: Paradox or contradiction constructions.
        callback_count: Explicit references back to earlier points.
    """

    self_deprecation_count: int
    paradox_count: int
    callback_count: int


@dataclass(frozen=True)
class VocabularyFingerprint:
    """Vocabulary fingerprint for a voice.

    Attributes:
        distinctive_words: High TF-IDF words relative to the corpus.
        coined_terms: Words absent from the reference word set.
        recurring_phrases: Bigrams appearing multiple times.
    """

    distinctive_words: tuple[str, ...]
    coined_terms: tuple[str, ...]
    recurring_phrases: tuple[str, ...]


@dataclass(frozen=True)
class PunctuationHabits:
    """Punctuation usage frequencies (per 1 000 words).

    Attributes:
        em_dash_frequency: Em-dash (``—`` or ``--``) occurrences.
        ellipsis_frequency: Ellipsis (``...`` or ``…``) occurrences.
        parenthetical_frequency: Parenthetical aside occurrences.
        exclamation_frequency: Exclamation mark occurrences.
    """

    em_dash_frequency: float
    ellipsis_frequency: float
    parenthetical_frequency: float
    exclamation_frequency: float


@dataclass(frozen=True)
class VoicePatterns:
    """All extracted voice patterns for a set of exemplar texts.

    Attributes:
        sentence_metrics: Sentence structure analysis.
        paragraph_metrics: Paragraph structure analysis.
        transitions: Detected transition words with counts, sorted by
            descending frequency.
        metaphor_families: Detected metaphor domains.
        rhetorical_moves: Self-deprecation, paradox, and callback counts.
        vocabulary: Vocabulary fingerprint (TF-IDF, coinages, phrases).
        punctuation: Punctuation habit frequencies.
        citation_density: Citation / quotation / reference signals per ~1000
            words, aggregated across the corpus weighted by each text's
            audience authority. ``0.0`` when the corpus cites nothing.
    """

    sentence_metrics: SentenceMetrics
    paragraph_metrics: ParagraphMetrics
    transitions: tuple[tuple[str, int], ...]
    metaphor_families: tuple[MetaphorFamily, ...]
    rhetorical_moves: RhetoricalMoves
    vocabulary: VocabularyFingerprint
    punctuation: PunctuationHabits
    citation_density: float = 0.0


# ---- Sentence helpers ----


def _split_sentences(text: str) -> list[str]:
    """Split *text* into sentences using terminal-punctuation heuristics."""
    stripped = text.strip()
    if not stripped:
        return []
    parts = _SENTENCE_SPLIT_RE.split(stripped)
    return [s.strip() for s in parts if s.strip()]


def _count_words(text: str) -> int:
    """Return the whitespace-delimited token count of *text*."""
    return len(text.split())


def _is_question(sentence: str) -> bool:
    """Return whether *sentence* ends with a question mark."""
    return sentence.rstrip().endswith("?")


def _classify_length(word_count: int) -> str:
    """Classify a sentence as ``'short'``, ``'medium'``, or ``'long'``."""
    if word_count <= _SHORT_SENTENCE_MAX:
        return "short"
    if word_count >= _LONG_SENTENCE_MIN:
        return "long"
    return "medium"


def _compute_sentence_metrics(all_sentences: list[str]) -> SentenceMetrics:
    """Compute sentence-level metrics from a flat list of sentences."""
    total = len(all_sentences)
    if total == 0:
        return SentenceMetrics(
            average_length=0.0,
            short_count=0,
            medium_count=0,
            long_count=0,
            fragment_count=0,
            question_frequency=0.0,
            total_sentences=0,
        )
    lengths = [_count_words(s) for s in all_sentences]
    avg = sum(lengths) / total
    buckets = Counter(_classify_length(n) for n in lengths)
    fragments = sum(1 for n in lengths if n <= _FRAGMENT_MAX_WORDS)
    questions = sum(1 for s in all_sentences if _is_question(s))
    return SentenceMetrics(
        average_length=avg,
        short_count=buckets.get("short", 0),
        medium_count=buckets.get("medium", 0),
        long_count=buckets.get("long", 0),
        fragment_count=fragments,
        question_frequency=questions / total,
        total_sentences=total,
    )


# ---- Paragraph helpers ----


def _split_paragraphs(text: str) -> list[str]:
    """Split *text* into paragraphs on double-newline boundaries."""
    parts = re.split(r"\n\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _compute_paragraph_metrics(texts: list[str]) -> ParagraphMetrics:
    """Compute paragraph-level metrics across multiple texts."""
    all_paragraphs: list[str] = []
    for text in texts:
        all_paragraphs.extend(_split_paragraphs(text))
    if not all_paragraphs:
        return ParagraphMetrics(average_sentences_per_paragraph=0.0)
    counts = [len(_split_sentences(p)) for p in all_paragraphs]
    avg = sum(counts) / len(counts)
    return ParagraphMetrics(average_sentences_per_paragraph=avg)


# ---- Transition helpers ----


def _count_transitions(combined: str) -> tuple[tuple[str, int], ...]:
    """Count transition word occurrences in *combined* text."""
    found: list[tuple[str, int]] = []
    for word, pattern in _TRANSITION_PATTERNS:
        count = len(pattern.findall(combined))
        if count > 0:
            found.append((word, count))
    return tuple(sorted(found, key=lambda t: (-t[1], t[0])))


# ---- Rhetorical helpers ----


def _count_pattern_matches(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> int:
    """Count total matches of *patterns* in *text*."""
    return sum(len(p.findall(text)) for p in patterns)


def _compute_rhetorical_moves(combined: str) -> RhetoricalMoves:
    """Detect rhetorical move patterns in *combined* text."""
    return RhetoricalMoves(
        self_deprecation_count=_count_pattern_matches(
            combined,
            _SELF_DEPRECATION_PATTERNS,
        ),
        paradox_count=_count_pattern_matches(combined, _PARADOX_PATTERNS),
        callback_count=_count_pattern_matches(combined, _CALLBACK_PATTERNS),
    )


# ---- Punctuation helpers ----


def _frequency_per_thousand(count: int, total_words: int) -> float:
    """Return *count* normalised to per-1000-word frequency."""
    if total_words == 0:
        return 0.0
    return count / total_words * _PER_THOUSAND


def _compute_punctuation(combined: str, total_words: int) -> PunctuationHabits:
    """Quantify punctuation habits in *combined* text."""
    return PunctuationHabits(
        em_dash_frequency=_frequency_per_thousand(
            len(_EM_DASH_RE.findall(combined)),
            total_words,
        ),
        ellipsis_frequency=_frequency_per_thousand(
            len(_ELLIPSIS_RE.findall(combined)),
            total_words,
        ),
        parenthetical_frequency=_frequency_per_thousand(
            len(_PARENTHETICAL_RE.findall(combined)),
            total_words,
        ),
        exclamation_frequency=_frequency_per_thousand(
            len(_EXCLAMATION_RE.findall(combined)),
            total_words,
        ),
    )


def _count_quotation_spans(text: str) -> int:
    """Count source-quotation spans (quoted spans of at least 4 words).

    The word-count floor (:data:`_MIN_QUOTE_WORDS`) filters dialogue tags,
    scare quotes, and inline code, which are short quoted spans rather than
    quoted source material.
    """
    return sum(
        1
        for span in _QUOTATION_SPAN_RE.findall(text)
        if len(span.split()) >= _MIN_QUOTE_WORDS
    )


def _count_citation_signals(text: str) -> int:
    """Return the number of citation / quotation / reference signals in *text*.

    Sums five independent signal families: source-quotation spans (see
    :func:`_count_quotation_spans`), blockquote lines, attribution phrases,
    reference markers (``[1]`` / author-year), and outbound source links.

    The families are counted **independently and by design** — a single dense
    construct (e.g. a blockquote that both attributes a source and links to it)
    legitimately fires several counters, because layering quotation, attribution
    and a link *is* denser citation behaviour than any one alone. The signal is
    a soft voice-shaping rate, not an exact count, so this compounding is the
    intended behaviour rather than double-counting to be deduplicated.
    """
    return (
        _count_quotation_spans(text)
        + len(_BLOCKQUOTE_LINE_RE.findall(text))
        + len(_ATTRIBUTION_RE.findall(text))
        + len(_REFERENCE_MARKER_RE.findall(text))
        + len(_OUTBOUND_LINK_RE.findall(text))
    )


def _compute_citation_density(
    texts: list[str],
    weights: list[float] | None,
) -> float:
    """Return the audience-weighted citation density across *texts*.

    Each text contributes its own per-1000-word citation rate, combined as a
    weighted mean using *weights* (per-fragment audience authority). A
    ``None`` or wrong-length *weights* falls back to uniform weighting;
    negative weights are clamped to ``0.0``; a non-positive total weight
    yields ``0.0``.

    Args:
        texts: Exemplar body texts.
        weights: Per-text non-negative audience-authority weights, or ``None``.

    Returns:
        The weighted citation density (signals per ~1000 words).
    """
    if not texts:
        return 0.0
    if weights is None or len(weights) != len(texts):
        resolved = [1.0] * len(texts)
    else:
        resolved = [max(0.0, weight) for weight in weights]
    total_weight = sum(resolved)
    if total_weight <= 0:
        return 0.0
    accumulated = 0.0
    for text, weight in zip(texts, resolved, strict=True):
        density = _frequency_per_thousand(
            _count_citation_signals(text), _count_words(text)
        )
        accumulated += weight * density
    return accumulated / total_weight


# ---- TF-IDF helpers ----


def _tokenize_words(text: str) -> list[str]:
    """Lowercase and extract alphabetic tokens from *text*."""
    return [w.lower() for w in re.findall(r"[a-zA-Z]+", text)]


def _compute_tfidf(documents: list[list[str]]) -> dict[str, float]:
    """Compute TF-IDF scores across *documents* with smoothed IDF.

    Each document is a list of lowercase tokens. Returns a mapping from
    token to its aggregated TF-IDF score (summed across documents).

    Uses the smoothed IDF formula ``log(1 + n_docs / (1 + doc_freq))`` so
    that single-document corpora still produce non-zero, rank-preserving
    scores. (The unsmoothed formula collapses to ``log(1) == 0`` when
    ``n_docs == 1``.)

    Args:
        documents: Pre-tokenised document list.

    Returns:
        Token-to-score mapping, higher means more distinctive.
    """
    n_docs = len(documents)
    if n_docs == 0:
        return {}
    doc_freq: Counter[str] = Counter()
    for doc in documents:
        doc_freq.update(set(doc))
    scores: dict[str, float] = {}
    for doc in documents:
        tf_counts = Counter(doc)
        doc_len = len(doc)
        if doc_len == 0:
            continue
        for term, count in tf_counts.items():
            tf = count / doc_len
            idf = math.log(1 + n_docs / (1 + doc_freq[term]))
            scores[term] = scores.get(term, 0.0) + tf * idf
    return scores


def _find_recurring_bigrams(
    documents: list[list[str]],
) -> tuple[str, ...]:
    """Find bigrams appearing at least ``_MIN_PHRASE_OCCURRENCES`` times."""
    bigram_counts: Counter[str] = Counter()
    for doc in documents:
        for i in range(len(doc) - 1):
            bigram = f"{doc[i]} {doc[i + 1]}"
            bigram_counts[bigram] += 1
    return tuple(
        bigram
        for bigram, count in bigram_counts.most_common()
        if count >= _MIN_PHRASE_OCCURRENCES
    )


# ---- VoicePatternExtractor ----


class VoicePatternExtractor:
    """Extract voice patterns from exemplar body texts.

    Analyses sentence structure, paragraph structure, transition patterns,
    metaphor families, rhetorical moves, vocabulary fingerprint, and
    punctuation habits as specified in Section 11.1 of the Creek Ontology.

    Attributes:
        reference_words: Optional set of known English words. When
            provided, words not in the set are flagged as coined terms.
        top_n: Number of top distinctive words to include in the
            vocabulary fingerprint.
    """

    def __init__(
        self,
        *,
        reference_words: frozenset[str] | None = None,
        top_n: int = _DEFAULT_TOP_DISTINCTIVE,
    ) -> None:
        """Initialise the pattern extractor.

        Args:
            reference_words: Optional set of known English words for
                coined-term detection. When ``None``, coined terms
                analysis returns an empty tuple.
            top_n: Number of top TF-IDF words to return as distinctive.
        """
        self.reference_words = reference_words
        self.top_n = top_n
        # Pre-compute the lowercased reference set once; the English
        # dictionary can contain hundreds of thousands of words and this
        # avoids rebuilding it on every ``extract_vocabulary`` call.
        self._lower_ref: frozenset[str] | None = (
            frozenset(w.lower() for w in reference_words)
            if reference_words is not None
            else None
        )

    def extract_patterns(
        self,
        texts: list[str],
        *,
        weights: list[float] | None = None,
    ) -> VoicePatterns:
        """Extract all voice patterns from exemplar body texts.

        Args:
            texts: Body texts of voice exemplar fragments.
            weights: Optional per-text audience-authority weights, parallel
                to *texts*. Used only for ``citation_density`` so public-work
                citation habits dominate; ``None`` weights every text equally.

        Returns:
            A :class:`VoicePatterns` containing every extracted metric.
        """
        combined = "\n\n".join(texts)
        all_sentences = self._gather_sentences(texts)
        total_words = _count_words(combined)
        return VoicePatterns(
            sentence_metrics=_compute_sentence_metrics(all_sentences),
            paragraph_metrics=_compute_paragraph_metrics(texts),
            transitions=_count_transitions(combined),
            metaphor_families=tuple(self.extract_metaphors(texts)),
            rhetorical_moves=_compute_rhetorical_moves(combined),
            vocabulary=self.extract_vocabulary(texts),
            punctuation=_compute_punctuation(combined, total_words),
            citation_density=_compute_citation_density(texts, weights),
        )

    def extract_metaphors(self, texts: list[str]) -> list[MetaphorFamily]:
        """Detect recurring metaphor families in *texts*.

        Uses keyword lists from :data:`METAPHOR_DOMAINS` to identify
        metaphor domains, then collects matching keywords and example
        sentences for each detected domain.

        Args:
            texts: Body texts to scan for metaphor keywords.

        Returns:
            List of :class:`MetaphorFamily` for domains with at least
            one keyword match. Empty list when no matches are found.
        """
        if not texts:
            return []
        all_sentences = self._gather_sentences(texts)
        families: list[MetaphorFamily] = []
        for domain, keywords in METAPHOR_DOMAINS.items():
            result = self._scan_domain(domain, keywords, all_sentences)
            if result is not None:
                families.append(result)
        return families

    def extract_vocabulary(self, texts: list[str]) -> VocabularyFingerprint:
        """Generate a vocabulary fingerprint via TF-IDF analysis.

        Args:
            texts: Body texts to analyse.

        Returns:
            A :class:`VocabularyFingerprint` with distinctive words,
            coined terms, and recurring phrases.
        """
        if not texts:
            return VocabularyFingerprint(
                distinctive_words=(),
                coined_terms=(),
                recurring_phrases=(),
            )
        documents = [_tokenize_words(t) for t in texts]
        distinctive = self._top_distinctive(documents)
        coined = self._detect_coined_terms(documents)
        recurring = _find_recurring_bigrams(documents)
        return VocabularyFingerprint(
            distinctive_words=distinctive,
            coined_terms=coined,
            recurring_phrases=recurring,
        )

    # ---- Private helpers ----

    @staticmethod
    def _gather_sentences(texts: list[str]) -> list[str]:
        """Collect all sentences across multiple texts."""
        sentences: list[str] = []
        for text in texts:
            sentences.extend(_split_sentences(text))
        return sentences

    @staticmethod
    def _scan_domain(
        domain: str,
        keywords: tuple[str, ...],
        sentences: list[str],
    ) -> MetaphorFamily | None:
        """Scan *sentences* for *keywords* and return a family if found."""
        matched: set[str] = set()
        examples: list[str] = []
        for sentence in sentences:
            lower = sentence.lower()
            hits = [kw for kw in keywords if re.search(rf"\b{kw}\b", lower)]
            if hits:
                matched.update(hits)
                examples.append(sentence)
        if not matched:
            return None
        return MetaphorFamily(
            domain=domain,
            matches=tuple(sorted(matched)),
            example_sentences=tuple(examples),
        )

    def _top_distinctive(
        self,
        documents: list[list[str]],
    ) -> tuple[str, ...]:
        """Return the top distinctive words by TF-IDF score."""
        scores = _compute_tfidf(documents)
        ranked = sorted(scores, key=lambda w: -scores[w])
        return tuple(ranked[: self.top_n])

    def _detect_coined_terms(
        self,
        documents: list[list[str]],
    ) -> tuple[str, ...]:
        """Find words absent from the reference word set."""
        if self._lower_ref is None:
            return ()
        all_words = set(chain.from_iterable(documents))
        coined = sorted(w for w in all_words if w not in self._lower_ref)
        return tuple(coined)


# ---------------------------------------------------------------------------
# Voice register profile generation — Section 11.2
# ---------------------------------------------------------------------------


_PROFILE_SUBDIR: str = "07-Voice"
"""Vault subdirectory where register profile notes are persisted."""

_PROFILE_FILENAME_SUFFIX: str = "-profile.md"
"""Filename suffix for register profile markdown files."""

_RHETORICAL_SUBDIR: tuple[str, str] = ("07-Voice", "Rhetorical-Patterns")
"""Vault subdirectory where per-register rhetorical-pattern notes are persisted."""

_DEFAULT_MAX_EXEMPLARS_PER_PROFILE: int = 10
"""Default maximum number of exemplar passages included in a profile."""

_DEFAULT_MIN_EXEMPLARS_PER_PROFILE: int = 5
"""Registers below this threshold trigger a warning during profile generation."""

_REGISTER_VOICE_DESCRIPTIONS: dict[str, str] = {
    "confessional": (
        "Write in a confessional register. Use first person. "
        "Start from vulnerability and sensory detail, then arrive at insight. "
        "Let paragraphs build from concrete experience to abstract reflection. "
        "Favour em-dashes for asides."
    ),
    "analytical": (
        "Write in an analytical register. Use precise, qualified claims "
        "grounded in evidence. Move from observation to inference to "
        "implication. Metaphors illustrate; they do not carry the argument."
    ),
    "playful": (
        "Write in a playful register. Let wit carry the argument. "
        "Puns, reversals, and small jokes are welcome. Irony underlines "
        "sincerity rather than distancing from it."
    ),
    "prophetic": (
        "Write in a prophetic register. Speak with declarative clarity. "
        "Let each claim stand without apology. Cadence matters — "
        "short lines have weight."
    ),
    "instructional": (
        "Write in an instructional register. Lead the reader one step at a "
        "time. Define every term on first use. Resolve each paragraph to "
        "an actionable understanding."
    ),
    "raw": (
        "Write in a raw register. Preserve the rough edges of the thought. "
        "Let ideas arrive half-formed. Use fragments. Trust the reader to "
        "fill the gaps."
    ),
    "conversational": (
        "Write in a conversational register. Address the reader directly. "
        "Use the second person when natural. Keep paragraphs short so the "
        "exchange stays alive."
    ),
}
"""Register-specific opening lines for the generated LLM voice prompts."""

_REGISTER_ANTI_PATTERNS: dict[str, tuple[str, ...]] = {
    "confessional": (
        "Do not use corporate or management language.",
        "Do not hedge with qualifiers like 'perhaps' or 'possibly'.",
        "Do not structure thoughts as bullet points.",
    ),
    "analytical": (
        "Do not assert emotion without evidence.",
        "Do not adopt confessional first-person intimacy.",
        "Do not issue prophetic pronouncements.",
    ),
    "playful": (
        "Do not adopt formal academic solemnity.",
        "Do not use jargon without winking at it.",
        "Do not chain flat declarative sentences without variation.",
    ),
    "prophetic": (
        "Do not hedge or qualify the vision.",
        "Do not soften the claim with bureaucratic disclaimers.",
        "Do not diminish the statement with self-deprecation.",
    ),
    "instructional": (
        "Do not insert confessional asides that detour the reader.",
        "Do not leave jargon undefined on first use.",
        "Do not stop at metaphor without making the mechanism explicit.",
    ),
    "raw": (
        "Do not polish the rough edges into smoothness.",
        "Do not pre-empt the reader with explanatory framing.",
        "Do not translate the moment into academic language.",
    ),
    "conversational": (
        "Do not monologue without addressing the reader.",
        "Do not use academic distance or formal register.",
        "Do not bury the exchange under dense paragraphs.",
    ),
}
"""Register-specific list of constraints the voice should never violate."""


@dataclass(frozen=True)
class Exemplar:
    """A voice exemplar: a fragment's metadata paired with its body text.

    :class:`VoiceProfileGenerator` consumes these to compose register
    profiles. Bodies are kept separate from the :class:`Fragment` model
    because Fragment only stores frontmatter; the body lives in the
    markdown file alongside it.

    Attributes:
        fragment: The exemplar fragment's metadata.
        body: The exemplar's prose body as stored on disk.
    """

    fragment: Fragment
    body: str


def _exemplar_score(
    exemplar: Exemplar,
    weighting: VoiceAudienceWeightingConfig,
) -> float:
    """Return the audience-weighted ranking score for *exemplar*.

    Mirrors :meth:`VoiceExemplarCollector._score` but reads word count
    from the exemplar's body rather than a cached record: confidence
    base (3 for conviction, 2 for settled), +1 bonus for a body in the
    medium-length band [200, 800] words, and +1 bonus when every
    classification axis is populated. The base is then multiplied by
    :func:`audience_authority` so public-facing exemplars dominate.
    """
    fragment = exemplar.fragment
    score = _CONFIDENCE_SCORE.get(_confidence_value(fragment), 0)
    word_count = _word_count(exemplar.body)
    if _MEDIUM_LENGTH_MIN_WORDS <= word_count <= _MEDIUM_LENGTH_MAX_WORDS:
        score += _LENGTH_BONUS
    if _is_fully_classified(fragment):
        score += _CLASSIFICATION_BONUS
    return score * audience_authority(fragment, weighting)


@dataclass(frozen=True)
class VoiceProfile:
    """A per-register voice profile ready to be persisted as markdown.

    Attributes:
        register: The canonical voice register this profile describes.
        exemplars: Tuple of 5-10 sample passages that exemplify the
            register.
        patterns: The :class:`VoicePatterns` extracted from the passages.
        voice_prompt: A reusable LLM instruction block describing how to
            write in this register.
        anti_patterns: The register's "DO NOT" constraints.
        generated_at: Timezone-aware timestamp of profile generation.
    """

    register: str
    exemplars: tuple[str, ...]
    patterns: VoicePatterns
    voice_prompt: str
    anti_patterns: tuple[str, ...]
    generated_at: datetime

    @property
    def exemplar_count(self) -> int:
        """Return the number of sample passages included in the profile."""
        return len(self.exemplars)


def _format_structural_patterns(patterns: VoicePatterns) -> list[str]:
    """Render the structural pattern section as markdown bullet lines."""
    sm = patterns.sentence_metrics
    pm = patterns.paragraph_metrics
    lines = [
        f"- Average sentence length: {sm.average_length:.1f} words "
        f"(short {sm.short_count} / medium {sm.medium_count} / "
        f"long {sm.long_count}).",
        f"- Sentence fragments used as rhetorical beats: {sm.fragment_count}.",
        f"- Question frequency: {sm.question_frequency:.2f}.",
        f"- Average sentences per paragraph: {pm.average_sentences_per_paragraph:.1f}.",
    ]
    if patterns.transitions:
        top = ", ".join(word for word, _ in patterns.transitions[:5])
        lines.append(f"- Preferred transitions: {top}.")
    return lines


def _format_metaphor_families(patterns: VoicePatterns) -> list[str]:
    """Render the metaphor family section as markdown bullet lines."""
    if not patterns.metaphor_families:
        return ["- No recurring metaphor families detected."]
    lines: list[str] = []
    for family in patterns.metaphor_families:
        matches = ", ".join(family.matches)
        lines.append(f"- **{family.domain}**: {matches}.")
    return lines


def _format_rhetorical_moves(patterns: VoicePatterns) -> list[str]:
    """Render the rhetorical moves section as markdown bullet lines."""
    moves = patterns.rhetorical_moves
    return [
        f"- Self-deprecation before insight: {moves.self_deprecation_count}.",
        f"- Paradox constructions: {moves.paradox_count}.",
        f"- Callbacks to earlier points: {moves.callback_count}.",
    ]


def _build_voice_prompt(
    register: str,
    patterns: VoicePatterns,
    anti_patterns: tuple[str, ...],
) -> str:
    """Construct the LLM-ready voice prompt for a register.

    Args:
        register: Canonical register name.
        patterns: Extracted :class:`VoicePatterns` for this register.
        anti_patterns: The register's DO NOT constraints.

    Returns:
        A multi-paragraph instruction block an LLM can follow to write
        in this register.
    """
    lines: list[str] = [_REGISTER_VOICE_DESCRIPTIONS[register], ""]
    if patterns.metaphor_families:
        domains = ", ".join(f.domain for f in patterns.metaphor_families)
        lines.append(f"Metaphors surface from: {domains}.")
    if patterns.citation_density >= _CITATION_PROMPT_THRESHOLD:
        lines.append(
            "Reference and quote your sources — you cite at roughly "
            f"{patterns.citation_density:.1f} markers per 1000 words; "
            "ground claims in attributed quotations and references.",
        )
    moves = patterns.rhetorical_moves
    if moves.paradox_count > 0:
        lines.append("Use paradox constructions to hold tension without resolving it.")
    if moves.callback_count > 0:
        lines.append("Call back to earlier points to build narrative continuity.")
    if moves.self_deprecation_count > 0:
        lines.append(
            "Allow brief self-deprecation before arriving at an insight.",
        )
    if patterns.vocabulary.recurring_phrases:
        phrases = ", ".join(patterns.vocabulary.recurring_phrases[:3])
        lines.append(f"Lean on recurring phrases such as: {phrases}.")
    lines.extend(("", f"NEVER: {' '.join(anti_patterns)}"))
    return "\n".join(lines)


class VoiceProfileGenerator:
    """Compose per-register voice profiles from exemplars and patterns.

    The generator realises Section 11.2 of the Creek Ontology: for each
    voice register, produce a profile note combining 5-10 exemplar
    passages, the extracted :class:`VoicePatterns`, a reusable LLM voice
    prompt, and register-specific anti-patterns.

    Attributes:
        max_exemplars: Upper bound on exemplars retained per profile.
        min_exemplars: Threshold below which a warning is logged.
    """

    def __init__(
        self,
        *,
        max_exemplars: int = _DEFAULT_MAX_EXEMPLARS_PER_PROFILE,
        min_exemplars: int = _DEFAULT_MIN_EXEMPLARS_PER_PROFILE,
        collector: VoiceExemplarCollector | None = None,
        extractor: VoicePatternExtractor | None = None,
        override: PrivacyTierOverride = PrivacyTierOverride.ALL,
        audience_weighting: VoiceAudienceWeightingConfig | None = None,
    ) -> None:
        """Initialise the generator.

        Args:
            max_exemplars: Upper bound on exemplars per profile. Must be
                at least 1.
            min_exemplars: Lower bound below which a warning is logged.
                Must be at least 1 and no greater than ``max_exemplars``.
            collector: Optional :class:`VoiceExemplarCollector` used by
                :meth:`generate_all_profiles`. Defaults to a collector
                configured with the same exemplar bounds. **An injected
                collector keeps its own** ``override``: it is a fully
                configured object the caller built deliberately, and
                silently overwriting one of its privacy settings from a
                sibling argument would be the more surprising behaviour.
            extractor: Optional :class:`VoicePatternExtractor` used by
                :meth:`generate_all_profiles`. Defaults to a plain
                extractor.
            override: Tier ceiling for the corpus walk (#968), forwarded
                into the *default* collector only (see *collector*).
                Defaults to
                :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
                meaning "no ceiling declared", so it is a genuine no-op for
                callers that predate #968. The default is safe because both
                production report surfaces state an override explicitly,
                which ``tests/test_mcp_report_tier_ceiling.py``'s
                ``test_production_report_callers_always_state_an_override``
                enforces structurally. The value is *not* stored on the
                generator: :meth:`_stream_into` reads it back off
                ``self._collector`` so there is exactly one storage location
                and an injected collector cannot be contradicted.
            audience_weighting: Graduated audience-authority multipliers
                applied during ranking, forwarded into the *default*
                collector only (see *collector*), on the same terms as
                *override*: the value is **not** stored on the generator.
                :meth:`_rank_exemplars` and :meth:`_stream_into` read it back
                off ``self._collector`` so there is exactly one storage
                location and an injected collector cannot be contradicted.
                ``None`` means "the standard weighting" —
                :class:`~creek.config.VoiceAudienceWeightingConfig`'s defaults,
                which is *enabled*. That is the opposite of
                :func:`~creek.generate.ai_style.fingerprint.build_fingerprint`,
                whose ``None`` means off; see that function's docstring for
                why the two differ and why neither default moved (#1313).
                No production caller *on this path* relies on the default:
                ``tests/test_mcp_report_tier_ceiling.py``'s
                ``test_production_voice_callers_always_state_an_audience_weighting``
                requires every construction of this class,
                :class:`VoiceExemplarCollector` and
                :func:`generate_register_samples` to pass the vault's config
                explicitly. That guard covers those three symbols only — it
                says nothing about
                :func:`~creek.generate.ai_style.fingerprint.build_fingerprint`,
                which has a production caller still taking its default
                (see #1410).

        Raises:
            ValueError: If either bound is less than 1, or if
                ``min_exemplars`` exceeds ``max_exemplars``.
        """
        if max_exemplars < 1:
            msg = f"max_exemplars must be >= 1, got {max_exemplars}"
            raise ValueError(msg)
        if min_exemplars < 1:
            msg = f"min_exemplars must be >= 1, got {min_exemplars}"
            raise ValueError(msg)
        if min_exemplars > max_exemplars:
            msg = f"min_exemplars ({min_exemplars}) > max_exemplars ({max_exemplars})"
            raise ValueError(msg)
        self.max_exemplars = max_exemplars
        self.min_exemplars = min_exemplars
        self._collector = collector or VoiceExemplarCollector(
            max_per_register=max_exemplars,
            min_per_register=min_exemplars,
            override=override,
            audience_weighting=audience_weighting,
        )
        self._extractor = extractor or VoicePatternExtractor()

    def generate_profile(
        self,
        register: str,
        exemplars: Sequence[Exemplar],
        patterns: VoicePatterns,
    ) -> VoiceProfile:
        """Compose a :class:`VoiceProfile` from exemplars and patterns.

        Passages are taken from the first :attr:`max_exemplars` entries
        of *exemplars* so callers can pre-rank before invoking. The
        generated voice prompt is register-specific and incorporates
        detected metaphor families and rhetorical moves. Registers with
        fewer than :attr:`min_exemplars` samples log a warning but still
        produce a profile.

        Args:
            register: One of the seven canonical voice registers.
            exemplars: Ordered sequence of exemplars; the top
                :attr:`max_exemplars` are retained.
            patterns: :class:`VoicePatterns` extracted from the
                exemplars' bodies.

        Returns:
            A fully populated :class:`VoiceProfile`.

        Raises:
            ValueError: If *register* is not one of the seven canonical
                voice registers, or if *exemplars* is empty.
        """
        if register not in VOICE_REGISTERS:
            msg = f"Unknown voice register: {register!r}"
            raise ValueError(msg)
        if not exemplars:
            msg = f"Cannot generate profile for {register!r}: exemplars is empty"
            raise ValueError(msg)
        selected = list(exemplars)[: self.max_exemplars]
        if len(selected) < self.min_exemplars:
            logger.warning(
                "Voice register %r has only %d exemplar(s) for profile (minimum %d).",
                register,
                len(selected),
                self.min_exemplars,
            )
        anti_patterns = _REGISTER_ANTI_PATTERNS[register]
        voice_prompt = _build_voice_prompt(register, patterns, anti_patterns)
        return VoiceProfile(
            register=register,
            exemplars=tuple(e.body for e in selected),
            patterns=patterns,
            voice_prompt=voice_prompt,
            anti_patterns=anti_patterns,
            generated_at=datetime.now(tz=UTC),
        )

    def write_profile(
        self,
        profile: VoiceProfile,
        vault_path: Path,
    ) -> Path:
        """Persist *profile* to ``07-Voice/<register>-profile.md``.

        The output follows the Section 11.2 template with the required
        markdown headings and a YAML frontmatter block capturing the
        profile metadata.

        Args:
            profile: The profile to write.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Absolute path of the written markdown file.

        Raises:
            ValueError: If ``profile.register`` is not one of the seven
                canonical voice registers. :class:`VoiceProfile` is a
                public dataclass, so this guard prevents path traversal
                when a caller constructs a profile directly.
        """
        if profile.register not in VOICE_REGISTERS:
            msg = f"Unknown voice register on profile: {profile.register!r}"
            raise ValueError(msg)
        profile_dir = vault_path / _PROFILE_SUBDIR
        profile_dir.mkdir(parents=True, exist_ok=True)
        target = profile_dir / f"{profile.register}{_PROFILE_FILENAME_SUFFIX}"
        body = self._render_profile_body(profile)
        post = frontmatter.Post(
            content=body,
            type="voice_profile",
            register=profile.register,
            exemplar_count=profile.exemplar_count,
            generated_date=profile.generated_at.isoformat(),
            tags=["voice", "voice-profile", profile.register],
        )
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target

    def generate_all_profiles(self, vault_path: Path) -> list[Path]:
        """Generate profile notes for every register with exemplars.

        Scans ``01-Fragments/`` lazily, routing each qualifying fragment
        to a per-register JSONL accumulator on disk and dropping its
        body before reading the next file. Memory therefore peaks at
        the largest single register, not the whole vault — a 10 000-
        fragment vault stays well under the 200 MB envelope mandated
        by PERF-004.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            List of paths written, one per register with exemplars.
        """
        written: list[Path] = []
        for register, ranked, patterns in self._iter_register_patterns(vault_path):
            profile = self.generate_profile(register, ranked, patterns)
            written.append(self.write_profile(profile, vault_path))
        return written

    def generate_rhetorical_patterns(self, vault_path: Path) -> list[Path]:
        """Persist a per-register rhetorical-patterns note (#582).

        Reuses the same streaming exemplar collection and pattern extraction as
        :meth:`generate_all_profiles` (via :meth:`_iter_register_patterns`), but
        writes only the ontology's "### Rhetorical Moves" section — computed by
        :func:`_compute_rhetorical_moves` through the pattern extractor — to
        ``07-Voice/Rhetorical-Patterns/<register>.md``. No move-detection logic
        is duplicated.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            List of paths written, one per register with exemplars.
        """
        return [
            self._write_rhetorical_note(register, patterns, vault_path)
            for register, _ranked, patterns in self._iter_register_patterns(vault_path)
        ]

    # ---- Private helpers ----

    def _iter_register_patterns(
        self,
        vault_path: Path,
    ) -> Iterator[tuple[str, list[Exemplar], VoicePatterns]]:
        """Yield ``(register, ranked_exemplars, patterns)`` per non-empty register.

        The single streaming exemplar-collection + ranking + pattern-extraction
        path shared by :meth:`generate_all_profiles` and
        :meth:`generate_rhetorical_patterns`. Memory peaks at the largest single
        register: each register's working set is released before the next is
        loaded (PERF-004).

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Yields:
            ``(register, ranked exemplars, extracted patterns)`` for every
            register that has qualifying exemplars.
        """
        with self._streaming_accumulator(vault_path) as register_paths:
            for register in VOICE_REGISTERS:
                jsonl_path = register_paths.get(register)
                if jsonl_path is None or not jsonl_path.exists():
                    continue
                register_exemplars = list(_iter_exemplars_from_jsonl(jsonl_path))
                if not register_exemplars:
                    continue
                ranked = self._rank_exemplars(register_exemplars)
                bodies = [e.body for e in ranked]
                weights = [
                    audience_authority(e.fragment, self._collector.audience_weighting)
                    for e in ranked
                ]
                patterns = self._extractor.extract_patterns(bodies, weights=weights)
                yield register, ranked, patterns
                del register_exemplars, ranked, bodies, patterns

    def _write_rhetorical_note(
        self,
        register: str,
        patterns: VoicePatterns,
        vault_path: Path,
    ) -> Path:
        """Write *register*'s rhetorical-moves note and return its path."""
        target_dir = vault_path.joinpath(*_RHETORICAL_SUBDIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{register}.md"
        body = "\n".join(
            [
                f"# Rhetorical Patterns — {register}",
                "",
                "### Rhetorical Moves",
                "",
                *_format_rhetorical_moves(patterns),
                "",
            ],
        )
        post = frontmatter.Post(
            content=body,
            type="rhetorical_patterns",
            register=register,
            generated_date=datetime.now(tz=UTC).isoformat(),
            tags=["voice", "rhetorical-patterns", register],
        )
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target

    @contextmanager
    def _streaming_accumulator(
        self,
        vault_path: Path,
    ) -> Iterator[dict[str, Path]]:
        """Stream qualifying fragments into per-register JSONL files.

        Yields a mapping of ``register -> jsonl_path`` for use within
        :meth:`generate_all_profiles`. The temp directory is deleted on
        context-manager exit so partial runs leave no debris.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Yields:
            Mapping of canonical register name to the absolute path of
            its JSONL accumulator file. Registers with no qualifying
            fragments are absent from the mapping.
        """
        with tempfile.TemporaryDirectory(prefix="creek-voice-") as tmp:
            tmp_path = Path(tmp)
            register_paths: dict[str, Path] = {}
            register_handles: dict[str, _JsonlWriter] = {}
            try:
                fragments_dir = vault_path / _FRAGMENTS_SUBDIR
                if fragments_dir.is_dir():
                    self._stream_into(
                        fragments_dir,
                        tmp_path,
                        register_paths,
                        register_handles,
                    )
            finally:
                for handle in register_handles.values():
                    handle.close()
            yield register_paths

    def _stream_into(
        self,
        fragments_dir: Path,
        tmp_path: Path,
        register_paths: dict[str, Path],
        register_handles: dict[str, _JsonlWriter],
    ) -> None:
        """Walk *fragments_dir* and append eligible exemplars to JSONL files.

        Both privacy settings are read back off ``self._collector`` rather
        than duplicated onto the generator, so an injected collector governs
        this walk exactly as it governs the collector's own (#968).
        """
        for md_file in sorted(fragments_dir.rglob("*.md")):
            # Defensive path gate (Issue #466): skip borrowed / AI-authored
            # content under ``11-Other-Authors`` so it never reaches the
            # streaming voice-profile corpus, mirroring ``collect_exemplars``.
            if _is_other_authors_path(md_file):
                continue
            loaded = _load_fragment_with_body(md_file)
            if loaded is None:
                continue
            fragment, body, raw = loaded
            # Ceiling gate (#968), additive to the allow_intimate consent
            # gate below — see ``VoiceExemplarCollector._eligible_register``.
            if not within_ceiling(raw, self._collector.override):
                continue
            register = _eligible_register(
                fragment,
                allow_intimate=self._collector.allow_intimate,
            )
            if register is None:
                continue
            handle = register_handles.get(register)
            if handle is None:
                jsonl_path = tmp_path / f"{register}.jsonl"
                handle = _JsonlWriter(jsonl_path)
                register_handles[register] = handle
                register_paths[register] = jsonl_path
            handle.append(
                {
                    "fragment": fragment.model_dump(mode="json"),
                    "body": body,
                }
            )
            # Drop the local body reference before the next iteration
            # so the per-iteration peak is bounded by a single fragment.
            del body, fragment, raw, loaded

    def _rank_exemplars(self, exemplars: list[Exemplar]) -> list[Exemplar]:
        """Rank *exemplars* by the same quality heuristic as the collector.

        Scoring mirrors :meth:`VoiceExemplarCollector.rank_exemplars` but
        operates on :class:`Exemplar` values, avoiding coupling to the
        collector's internal cache. Ties are broken by ascending
        fragment id for deterministic ordering.

        Returns:
            Up to :attr:`max_exemplars` exemplars, sorted by descending
            score.
        """
        scored = sorted(
            exemplars,
            key=lambda e: (
                -_exemplar_score(e, self._collector.audience_weighting),
                e.fragment.id,
            ),
        )
        return scored[: self.max_exemplars]

    @staticmethod
    def _render_profile_body(profile: VoiceProfile) -> str:
        """Render the markdown body of a voice profile note."""
        lines: list[str] = [
            f"# Voice Proxy: {profile.register.title()}",
            "",
            "## Prompt for LLM",
            "",
            profile.voice_prompt,
            "",
            "### Structural Patterns",
            "",
            *_format_structural_patterns(profile.patterns),
            "",
            "### Metaphor Families",
            "",
            *_format_metaphor_families(profile.patterns),
            "",
            "### Rhetorical Moves",
            "",
            *_format_rhetorical_moves(profile.patterns),
            "",
            "### Sample Passages",
            "",
        ]
        for index, passage in enumerate(profile.exemplars, start=1):
            lines.extend((f"{index}. {passage}", ""))
        lines.extend(("### Anti-Patterns (DO NOT)", ""))
        lines.extend(f"- {pattern}" for pattern in profile.anti_patterns)
        lines.append("")
        return "\n".join(lines)


def generate_register_samples(
    vault_path: Path,
    *,
    override: PrivacyTierOverride = PrivacyTierOverride.ALL,
    audience_weighting: VoiceAudienceWeightingConfig | None = None,
    on_prune: Callable[[Path], None] | None = None,
) -> dict[str, Path]:
    """Collect the voice corpus and persist its register samples (#879).

    Mirrors :func:`creek.generate.lexicon.generate_lexicon`: **one**
    collector, built with the caller's ceiling, walks the vault and then
    writes what that same walk found. The single instance is load-bearing
    rather than stylistic — :meth:`VoiceExemplarCollector._persist_fragment`
    copies a fragment's source file only while the collector's record cache
    holds an entry for it, and only
    :meth:`VoiceExemplarCollector.collect_exemplars` fills that cache.
    Saving through a second collector still produces a full set of
    valid-looking exemplar notes, every one of them with an **empty body**.

    Only ``creek report --type voice`` calls this. The ``report_type="voice"``
    MCP tool still writes profiles alone; that divergence is a recorded
    decision tracked in issue #1204, not an oversight to tidy up by wiring
    the MCP surface here.

    Args:
        vault_path: Root of the Obsidian vault.
        override: Tier ceiling for the corpus walk (#968), forwarded to the
            collector. Defaults to
            :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
            meaning "no ceiling declared", so the default is a genuine no-op
            for callers that predate #968. Additive to — never a replacement
            for — the collector's ``allow_intimate`` consent gate, which
            stays off here. The ceiling matters more on this path than on a
            rendered report: a persisted sample is a source fragment's file
            copied into the vault byte for byte.
        audience_weighting: Graduated audience-authority multipliers for the
            ranking, forwarded to the collector (#1313). ``None`` selects
            :class:`~creek.config.VoiceAudienceWeightingConfig`'s defaults, and
            the default exists only for library callers: every production
            caller must state the vault's own config, which
            ``tests/test_mcp_report_tier_ceiling.py``'s
            ``test_production_voice_callers_always_state_an_audience_weighting``
            enforces structurally. This knob cannot change *who* is eligible —
            :meth:`VoiceExemplarCollector.rank_exemplars` returns
            ``scored[:max_per_register]`` regardless of score, so unlike
            ``fingerprint.py``'s ``if weight > 0.0`` a zero authority here only
            de-ranks. It can still change *which* fragments survive the top-N
            cut, and because that cut decides whose file is copied verbatim,
            disabling the weighting can replace a self-authored sample with a
            borrowed one. That trade is documented in ``docs/configuration.md``.
        on_prune: Invoked once per deleted stale copy, with its path. This
            call both writes and *deletes* vault content, and the returned
            mapping describes only the writing, so a caller that reports
            what happened needs this to see the other half (#879).

    Returns:
        Mapping of register → path of the written ``_Summary.md``, one entry
        per register that had exemplars. Empty when the corpus is.
    """
    collector = VoiceExemplarCollector(
        override=override,
        audience_weighting=audience_weighting,
    )
    buckets = collector.collect_exemplars(vault_path)
    return collector.save_exemplars(buckets, vault_path, on_prune=on_prune)


__all__ = [
    "DEFAULT_MAX_PER_REGISTER",
    "DEFAULT_MIN_PER_REGISTER",
    "METAPHOR_DOMAINS",
    "VOICE_REGISTERS",
    "Exemplar",
    "MetaphorFamily",
    "ParagraphMetrics",
    "PunctuationHabits",
    "RhetoricalMoves",
    "SentenceMetrics",
    "VocabularyFingerprint",
    "VoiceExemplarCollector",
    "VoicePatternExtractor",
    "VoicePatterns",
    "VoiceProfile",
    "VoiceProfileGenerator",
    "generate_register_samples",
]
