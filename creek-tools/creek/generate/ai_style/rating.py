"""The end-state voice rating every writing path is judged by.

One question, one answer: does this finished text read as the owner, as
measured against the vault's voice fingerprint? The rating is the
fingerprint scan's ``voice_distance`` (see :mod:`creek.generate.ai_style.scanner`)
held against the configured ``voice_distance_upper``. It is deterministic,
makes no model call, and carries only numbers and closed feature keys, so it
can travel on any response or frontmatter without carrying prose.

The rating judges the *result*, not the edit. A rewrite that changed few
words but already reads as the owner is in voice; a heavily rewritten one in
generic model prose is not.

A vault whose fingerprint cannot judge (the subsystem disabled, no fingerprint
built, or one too thin to trust) gets no scorer at all, so a caller can never
mistake "not measured" for "in voice".

**Off-voice is logged, never refused.** Every writing path records each
finished text's rating with :func:`record_voice_rating`: one content-free JSON
line in ``00-Creek-Meta/voice-ratings.jsonl``, plus a ``WARNING`` on this
module's logger when the text is off-voice. The ledger is for monitoring,
alerting and tuning. Refusing an off-voice essay would discard work the owner
asked for and paid for, often when the point of asking was resonance with
the source rather than a perfect match to the fingerprint.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creek.audit import log as audit_log
from creek.config import CONFIG_LOAD_FAILURES, load_vault_config
from creek.generate.ai_style.fingerprint import load_fingerprint
from creek.generate.ai_style.scanner import scan

if TYPE_CHECKING:
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import ScanReport, VoiceFingerprint

logger = logging.getLogger(__name__)

VOICE_RATING_KEY = "voice_rating"
"""Response and frontmatter key under which a :class:`VoiceRating` travels."""

VOICE_RATINGS_RELPATH = Path("00-Creek-Meta/voice-ratings.jsonl")
"""Vault-relative ledger of every finished text's rating, one JSON line each."""


@dataclass(frozen=True)
class VoiceRating:
    """How one finished text measures against the owner's voice.

    Attributes:
        voice_distance: The fingerprint scan's distance, ``0.0`` to ``1.0``.
        voice_distance_upper: The vault's accepted ceiling.
        diverging: Feature keys of every tell family that fired, sorted.
        signature_gaps: Feature keys of owner signatures the text under-uses.
            Reported, never scored.
    """

    voice_distance: float
    voice_distance_upper: float
    diverging: tuple[str, ...]
    signature_gaps: tuple[str, ...]

    @property
    def in_voice(self) -> bool:
        """Whether the text sits at or under the vault's accepted ceiling."""
        return self.voice_distance <= self.voice_distance_upper

    def as_dict(self) -> dict[str, Any]:
        """Project the rating onto JSON-ready numbers and closed keys only."""
        return {
            "in_voice": self.in_voice,
            "voice_distance": round(self.voice_distance, 4),
            "voice_distance_upper": self.voice_distance_upper,
            "diverging": list(self.diverging),
            "signature_gaps": list(self.signature_gaps),
        }


def rating_from(report: ScanReport, config: AIStyleConfig) -> VoiceRating:
    """Turn a scan *report* into the rating the vault's *config* defines.

    Args:
        report: A fingerprint scan of the finished text.
        config: The vault's AI-style configuration.

    Returns:
        The rating, with family keys deduplicated and sorted.
    """
    return VoiceRating(
        voice_distance=report.voice_distance,
        voice_distance_upper=config.voice_distance_upper,
        diverging=tuple(sorted({f.feature_key for f in report.findings})),
        signature_gaps=tuple(sorted({g.feature_key for g in report.signature_gaps})),
    )


@dataclass(frozen=True)
class VoiceScorer:
    """A vault's fingerprint and config, ready to rate finished text.

    Attributes:
        fingerprint: The owner's persisted, non-thin voice fingerprint.
        config: The vault's AI-style configuration.
    """

    fingerprint: VoiceFingerprint
    config: AIStyleConfig

    def report(self, text: str) -> ScanReport:
        """Scan *text* against the owner's fingerprint."""
        return scan(text, fingerprint=self.fingerprint, config=self.config)

    def rate(self, text: str) -> VoiceRating:
        """Rate *text*'s end state against the owner's voice."""
        return rating_from(self.report(text), self.config)


def load_voice_scorer(vault: Path) -> VoiceScorer | None:
    """Return the vault's scorer, or ``None`` when its voice cannot be judged.

    ``None`` when the vault config cannot load, the AI-style subsystem is
    disabled, no fingerprint has been built, or the fingerprint is thinner
    than ``min_fingerprint_fragments``. A thin fingerprint's softened score
    would pass prose it never measured.

    Args:
        vault: The vault root.

    Returns:
        A scorer, or ``None``.
    """
    try:
        config = load_vault_config(vault).ai_style
    except CONFIG_LOAD_FAILURES:
        return None
    if not config.enabled:
        return None
    fingerprint = load_fingerprint(vault, config)
    if fingerprint.fragment_count == 0 or fingerprint.is_thin(
        config.min_fingerprint_fragments
    ):
        return None
    return VoiceScorer(fingerprint, config)


def record_voice_rating(
    vault: Path,
    *,
    tool: str,
    rating: VoiceRating | None,
    attempts: int = 1,
    artifact: str | None = None,
) -> None:
    """Append one finished text's rating to the vault's voice-ratings ledger.

    Best-effort: a write failure is logged and swallowed, because monitoring
    must never cost the owner the text it measures. Lines are appended under
    the same per-path thread lock and ``flock`` as the MCP audit log
    (:func:`creek.audit.log.append_locked_line`), so every writing path can
    share the ledger without interleaving. The line carries no
    prose, only the tool, the rating's numbers and closed feature keys, and
    an optional vault-relative *artifact* path. An off-voice rating also logs
    a ``WARNING`` naming the tool and distance, for alerting.

    Args:
        vault: The vault root.
        tool: The writing path, e.g. ``"creek.revoice"`` or ``"creek draft"``.
        rating: The finished text's rating, or ``None`` when the vault's
            voice could not be judged (recorded as ``rated: false``).
        attempts: Model attempts behind the finished text.
        artifact: Vault-relative path of the saved result, when one exists.
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tool": tool,
        "rated": rating is not None,
        "attempts": attempts,
    }
    if rating is not None:
        entry |= rating.as_dict()
        if not rating.in_voice:
            logger.warning(
                "off-voice result from %s: voice_distance %.4f over %.4f",
                tool,
                rating.voice_distance,
                rating.voice_distance_upper,
            )
    if artifact is not None:
        entry["artifact"] = artifact
    try:
        audit_log.append_locked_line(
            vault / VOICE_RATINGS_RELPATH, json.dumps(entry, sort_keys=True)
        )
    except OSError as exc:
        logger.warning(
            "could not record a voice rating for %s: %s", tool, type(exc).__name__
        )
