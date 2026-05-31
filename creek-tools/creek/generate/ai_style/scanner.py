"""The scan engine: measure a draft against the user's voice fingerprint.

:func:`scan` runs every enabled, context-applicable tell, compares each
tell's measured rate to the user's stored rate for the same feature, and
produces a :class:`~creek.generate.ai_style.model.ScanReport` carrying the
divergence findings and the aggregate voice distance. The engine is pure
and deterministic; it knows nothing about how features are measured (the
tells do) or how the fingerprint was built (issue #419 does).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.generate.ai_style.distance import (
    FeatureContribution,
    bad_direction_magnitude,
    voice_distance,
)
from creek.generate.ai_style.model import Finding, ScanReport, Span
from creek.generate.ai_style.tells import get_tells

if TYPE_CHECKING:
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import Direction, VoiceFingerprint
    from creek.generate.ai_style.tells import Tell

_EXCERPT_PAD = 24
"""Characters of context to include on each side of a finding's span."""


def _line_of(text: str, index: int) -> int:
    """Return the 1-based line number of character *index* in *text*.

    Args:
        text: The body of text.
        index: A character offset into *text*.

    Returns:
        The 1-based line number containing *index*.
    """
    return text.count("\n", 0, index) + 1


def _excerpt(text: str, span: Span) -> str:
    """Return a short, single-line snippet around *span* for a finding.

    Args:
        text: The body of text.
        span: The character range to excerpt.

    Returns:
        The padded snippet with newlines collapsed to spaces.
    """
    start = max(0, span.start - _EXCERPT_PAD)
    end = min(len(text), span.end + _EXCERPT_PAD)
    return " ".join(text[start:end].split())


def _resolve_user_rate(
    tell: Tell,
    fingerprint: VoiceFingerprint,
) -> float:
    """Return the user's rate for *tell*'s feature, or its generic prior.

    Args:
        tell: The tell whose feature is being scored.
        fingerprint: The user's voice fingerprint.

    Returns:
        The fingerprint's measured rate when present, else the tell's
        documented generic prior (the sparse-corpus fallback).
    """
    measured = fingerprint.rate_for(tell.feature_key)
    return tell.generic_prior if measured is None else measured


def _findings_for(
    tell: Tell,
    text: str,
    *,
    draft_rate: float,
    user_rate: float,
    direction: Direction,
) -> list[Finding]:
    """Build findings for a tell that diverged past its margin.

    Locates occurrence spans via the tell; when it reports none (always
    the case for ``under``-use, which has no specific location), emits a
    single document-level finding so the divergence is still surfaced.

    Args:
        tell: The fired tell.
        text: The scanned text.
        draft_rate: The draft's measured rate.
        user_rate: The user's measured rate (or prior).
        direction: ``"over"`` or ``"under"``.

    Returns:
        One finding per located span, or a single doc-level finding.
    """
    message = (
        f"{tell.description} (draft {draft_rate:.2f} vs you {user_rate:.2f} "
        f"per 1k words; {direction}-use). Caveat: {tell.caveat}"
    )
    spans = tell.locate(text) if direction == "over" else []
    if not spans:
        spans = [Span(0, 0)]
    return [
        Finding(
            tell_id=tell.id,
            category=tell.category,
            feature_key=tell.feature_key,
            span=span,
            line=_line_of(text, span.start),
            excerpt=_excerpt(text, span) if span.end > span.start else "",
            draft_rate=draft_rate,
            user_rate=user_rate,
            direction=direction,
            message=message,
        )
        for span in spans
    ]


def scan(
    text: str,
    *,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
    context: str = "article",
) -> ScanReport:
    """Scan *text* against the user's voice *fingerprint*.

    Each enabled, context-applicable tell measures its feature in *text*;
    the divergence from the user's stored rate (or the tell's generic
    prior when the fingerprint is silent on that feature) feeds both the
    findings and the aggregate :func:`voice_distance`. When the fingerprint
    is thin, distance is softened and the report is flagged accordingly so
    callers do not over-react to an unreliable measurement.

    Args:
        text: The body of text to scan (frontmatter should be stripped by
            the caller; this operates on prose).
        fingerprint: The user's voice fingerprint (issue #419).
        config: The AI-style configuration.
        context: The scan context (``"article"`` or ``"comment"``);
            context-restricted tells are skipped when it does not match.

    Returns:
        A populated :class:`ScanReport`. Empty when ``config.enabled`` is
        ``False``.
    """
    if not config.enabled:
        return ScanReport()

    thin = fingerprint.is_thin(config.min_fingerprint_fragments)
    softening = config.thin_fingerprint_softening if thin else 1.0

    findings: list[Finding] = []
    deltas: dict[str, float] = {}
    contributions: list[FeatureContribution] = []

    for tell in get_tells(config.enabled_categories):
        if not tell.applies_in(context):
            continue
        draft_rate = tell.measure(text)
        user_rate = _resolve_user_rate(tell, fingerprint)
        deltas[tell.feature_key] = draft_rate - user_rate
        magnitude = bad_direction_magnitude(
            tell.polarity,
            draft_rate,
            user_rate,
        )
        weight = config.weight_for(
            category=tell.category,
            feature_key=tell.feature_key,
        )
        contributions.append(
            FeatureContribution(
                feature_key=tell.feature_key,
                weight=weight,
                magnitude=magnitude,
            ),
        )
        margin = config.default_margin if tell.margin is None else tell.margin
        if magnitude > margin:
            direction: Direction = "over" if tell.polarity == "avoid" else "under"
            findings.extend(
                _findings_for(
                    tell,
                    text,
                    draft_rate=draft_rate,
                    user_rate=user_rate,
                    direction=direction,
                ),
            )

    distance = voice_distance(contributions, softening=softening)
    return ScanReport(
        findings=findings,
        deltas=deltas,
        voice_distance=distance,
        thin_fingerprint=thin,
    )
