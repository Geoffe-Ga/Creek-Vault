"""Voice exemplar collection — gather high-confidence fragments by register.

Implements Section 11.1 of the Creek Ontology. The
:class:`VoiceExemplarCollector` scans the vault for fragments whose
``voice.confidence`` has crystallised (``settled`` or ``conviction``),
groups them by ``voice.register``, ranks them by quality, and writes
the top exemplars under ``07-Voice/Register-Samples/<register>/`` so a
voice proxy can later train against curated samples.

The collector is deliberately conservative about privacy: ``intimate``
tier fragments are excluded by default and only included when the caller
explicitly opts in. Saved exemplars include a per-register summary note
recording the breakdown of confidence levels.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
from pydantic import ValidationError

from creek.models import (
    Confidence,
    Fragment,
    Frequency,
    Mode,
    Phase,
    PrivacyTier,
    VoiceRegister,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

VOICE_REGISTERS: tuple[str, ...] = tuple(register.value for register in VoiceRegister)
"""All seven canonical voice registers from the Creek ontology."""

DEFAULT_MAX_PER_REGISTER: int = 20
"""Default cap on exemplars retained per register."""

DEFAULT_MIN_PER_REGISTER: int = 5
"""Registers with fewer than this many exemplars trigger a warning."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
"""Vault subdirectory scanned for fragment markdown files."""

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


def _load_fragment_with_body(
    md_file: Path,
) -> tuple[Fragment, str] | None:
    """Parse *md_file* into a Fragment and return it with its body text.

    Args:
        md_file: Markdown file to read.

    Returns:
        A ``(fragment, body)`` pair, or ``None`` when the file is not a
        valid fragment record (unreadable, wrong type, or invalid
        frontmatter).
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
        fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    return fragment, post.content


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
            participate in collection. Defaults to ``False``.
    """

    def __init__(
        self,
        *,
        max_per_register: int = DEFAULT_MAX_PER_REGISTER,
        min_per_register: int = DEFAULT_MIN_PER_REGISTER,
        allow_intimate: bool = False,
    ) -> None:
        """Initialise the collector.

        Args:
            max_per_register: Cap on exemplars retained per register.
                Must be at least 1.
            min_per_register: Minimum exemplars per register before a
                warning is emitted. Must be at least 1.
            allow_intimate: Whether to include ``intimate`` privacy tier
                fragments. Defaults to ``False``.

        Raises:
            ValueError: If ``max_per_register`` or ``min_per_register``
                is less than 1.
        """
        if max_per_register < 1:
            msg = f"max_per_register must be >= 1, got {max_per_register}"
            raise ValueError(msg)
        if min_per_register < 1:
            msg = f"min_per_register must be >= 1, got {min_per_register}"
            raise ValueError(msg)
        self.max_per_register = max_per_register
        self.min_per_register = min_per_register
        self.allow_intimate = allow_intimate
        self._records: dict[str, _ExemplarRecord] = {}

    # ---- Collection ----

    def collect_exemplars(self, vault_path: Path) -> dict[str, list[Fragment]]:
        """Scan ``01-Fragments/`` and group qualifying exemplars by register.

        Fragments are kept when their ``voice.confidence`` is ``settled``
        or ``conviction`` and their ``voice.register`` is set. Intimate
        privacy tier fragments are filtered out unless
        :attr:`allow_intimate` is ``True``. Each register key in the
        returned dict is always present; empty registers map to ``[]``.

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
            loaded = _load_fragment_with_body(md_file)
            if loaded is None:
                continue
            fragment, body = loaded
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

    def _eligible_register(self, fragment: Fragment) -> str | None:
        """Return the fragment's register if it qualifies, else ``None``."""
        if _confidence_value(fragment) not in _QUALIFYING_CONFIDENCE:
            return None
        if (
            not self.allow_intimate
            and str(fragment.privacy_tier) == PrivacyTier.INTIMATE.value
        ):
            return None
        register = _register_value(fragment)
        if register not in VOICE_REGISTERS:
            return None
        return register

    def _warn_below_minimum(self, buckets: dict[str, list[Fragment]]) -> None:
        """Emit a warning for any register short of :attr:`min_per_register`."""
        for register, frags in buckets.items():
            if len(frags) < self.min_per_register:
                logger.warning(
                    "Voice register %r has only %d exemplars (minimum %d).",
                    register,
                    len(frags),
                    self.min_per_register,
                )

    # ---- Ranking ----

    def rank_exemplars(self, fragments: list[Fragment]) -> list[Fragment]:
        """Rank *fragments* by quality and return the top ``max_per_register``.

        Scoring (max 5 per fragment):

        - ``conviction`` confidence: 3 points; ``settled``: 2 points.
        - Body length in ``[200, 800]`` words: +1 point (uses the cached
          word count from :meth:`collect_exemplars`; falls back to 0
          when the fragment was not previously collected).
        - All classification axes populated (frequency, wavelength
          phase / mode, voice register, voice confidence): +1 point.

        Ties are broken by descending ID for deterministic ordering.

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

    def _score(self, fragment: Fragment) -> int:
        """Return the integer ranking score for *fragment*."""
        score = _CONFIDENCE_SCORE.get(_confidence_value(fragment), 0)
        record = self._records.get(fragment.id)
        if record is not None and (
            _MEDIUM_LENGTH_MIN_WORDS <= record.word_count <= _MEDIUM_LENGTH_MAX_WORDS
        ):
            score += _LENGTH_BONUS
        if _is_fully_classified(fragment):
            score += _CLASSIFICATION_BONUS
        return score

    # ---- Persistence ----

    def save_exemplars(
        self,
        exemplars: dict[str, list[Fragment]],
        vault_path: Path,
    ) -> dict[str, Path]:
        """Copy ranked exemplars into ``07-Voice/Register-Samples/<register>/``.

        For each non-empty register the collector creates the destination
        folder, copies (or rewrites) up to :attr:`max_per_register` of
        the top-ranked fragments, and writes a ``_Summary.md`` note
        recording statistics about the cohort.

        Args:
            exemplars: Mapping of register → fragments, typically the
                output of :meth:`collect_exemplars`. Non-canonical
                register keys are skipped with a debug log entry.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Mapping of register → path of the written summary note for
            every non-empty register.
        """
        samples_root = vault_path.joinpath(*_SAMPLES_SUBPATH)
        summaries: dict[str, Path] = {}
        for register, fragments in exemplars.items():
            if register not in VOICE_REGISTERS:
                logger.debug("Skipping unknown voice register %r", register)
                continue
            if not fragments:
                continue
            ranked = self.rank_exemplars(fragments)
            register_dir = samples_root / register
            register_dir.mkdir(parents=True, exist_ok=True)
            for fragment in ranked:
                self._persist_fragment(fragment, register_dir)
            summaries[register] = self._write_summary(register, ranked, register_dir)
        return summaries

    def _persist_fragment(self, fragment: Fragment, register_dir: Path) -> Path:
        """Copy the source file for *fragment* (or serialise from memory)."""
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
        ranked: list[Fragment],
        register_dir: Path,
    ) -> Path:
        """Write the per-register summary note and return its path."""
        conviction = sum(
            1 for f in ranked if _confidence_value(f) == Confidence.CONVICTION.value
        )
        settled = sum(
            1 for f in ranked if _confidence_value(f) == Confidence.SETTLED.value
        )
        body = self._render_summary_body(register, ranked, conviction, settled)
        post = frontmatter.Post(
            content=body,
            type="voice-register-summary",
            voice_register=register,
            exemplar_count=len(ranked),
            conviction_count=conviction,
            settled_count=settled,
            generated_at=datetime.now(tz=UTC).isoformat(),
            tags=["voice", "voice-register", register],
        )
        summary_path = register_dir / _SUMMARY_FILENAME
        summary_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return summary_path

    @staticmethod
    def _render_summary_body(
        register: str,
        ranked: list[Fragment],
        conviction: int,
        settled: int,
    ) -> str:
        """Render the markdown body of a register summary note."""
        lines: list[str] = [
            f"# Voice Register: {register}",
            "",
            "## Statistics",
            "",
            f"- Exemplar count: {len(ranked)}",
            f"- Conviction confidence: {conviction}",
            f"- Settled confidence: {settled}",
            "",
            "## Exemplars",
            "",
        ]
        if ranked:
            for fragment in ranked:
                lines.append(f"- [[{fragment.id}|{fragment.title}]]")
        else:
            lines.append("_No exemplars collected._")
        lines.append("")
        return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_PER_REGISTER",
    "DEFAULT_MIN_PER_REGISTER",
    "VOICE_REGISTERS",
    "VoiceExemplarCollector",
]
