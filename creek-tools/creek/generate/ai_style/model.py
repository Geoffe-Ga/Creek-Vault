"""Core data types for the FEAT-040 AI-style / voice-fidelity subsystem.

These types are deliberately framework-only: they describe *what was
measured* and *how far it diverges from the user's voice*, without
knowing how any particular feature is computed (that lives in the
per-category detector modules added by later issues) or how the
fingerprint is built (issue #419 fills :class:`VoiceFingerprint`).

The headline concept is **voice distance**: a draft is scored by how far
its measured feature rates sit from the user's own measured rates, in
*either* direction. A feature where the draft matches the user
contributes nothing, no matter how "AI-ish" that feature is in general.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from creek.config import AIStyleCategory

Category = AIStyleCategory
"""The five tell families catalogued by the Wikipedia field guide.

Re-exported from :data:`creek.config.AIStyleCategory` so the config layer
and the detector framework share a single definition (and config can
validate against it without importing this package)."""

Direction = Literal["over", "under"]
"""Whether the draft over-uses a feature the user avoids (``over``) or
under-uses one the user characteristically employs (``under``)."""


@dataclass(frozen=True)
class Span:
    """A half-open character range ``[start, end)`` within a body of text."""

    start: int
    end: int


@dataclass(frozen=True)
class FeatureStat:
    """One measured feature of the user's writing.

    Attributes:
        rate: The user's measured rate for the feature (per 1000 words,
            unless the owning tell documents another unit).
        support: How many user-authored fragments backed the measurement.
            Used to soften flagging when the corpus is thin.
    """

    rate: float
    support: int


@dataclass(frozen=True)
class VoiceFingerprint:
    """A quantitative fingerprint of how the user actually writes.

    Issue #419 (the idiolect profiler) builds these from genuinely
    user-authored vault content; the framework only consumes them. A
    fingerprint maps each ``feature_key`` to a :class:`FeatureStat`.

    Attributes:
        features: Per-``feature_key`` measured statistics.
        fragment_count: Total user-authored fragments the fingerprint was
            built from. ``0`` denotes an empty / absent fingerprint.
    """

    features: dict[str, FeatureStat] = field(default_factory=dict)
    fragment_count: int = 0

    def __post_init__(self) -> None:
        """Defensively copy ``features`` so the fingerprint is not aliased.

        ``frozen=True`` blocks attribute reassignment but not mutation of a
        shared dict. The profiler (issue #419) builds a fresh dict, but a
        caller could pass a live one; copying on construction guarantees the
        fingerprint cannot change underneath a scan.
        """
        object.__setattr__(self, "features", self.features.copy())

    def rate_for(self, feature_key: str) -> float | None:
        """Return the user's measured rate for *feature_key*, or ``None``.

        Args:
            feature_key: The feature to look up.

        Returns:
            The measured rate, or ``None`` when the fingerprint has no
            observation for the feature (a sparse-corpus signal).
        """
        stat = self.features.get(feature_key)
        return None if stat is None else stat.rate

    def support_for(self, feature_key: str) -> int:
        """Return how many fragments backed *feature_key* (``0`` if absent).

        Args:
            feature_key: The feature to look up.

        Returns:
            The fragment support count, or ``0`` when unmeasured.
        """
        stat = self.features.get(feature_key)
        return 0 if stat is None else stat.support

    def is_thin(self, minimum: int) -> bool:
        """Return whether the fingerprint is too thin to trust.

        Args:
            minimum: The minimum fragment count for a reliable fingerprint.

        Returns:
            ``True`` when fewer than *minimum* fragments backed it.
        """
        return self.fragment_count < minimum


@dataclass(frozen=True)
class Finding:
    """One detected divergence between the draft and the user's voice.

    Attributes:
        tell_id: The :class:`~creek.generate.ai_style.tells.Tell` that fired.
        category: The tell's family.
        feature_key: The measured feature whose divergence triggered this.
        span: Where in the draft the finding sits. A doc-level finding uses
            ``Span(0, 0)``.
        line: 1-based line number of ``span.start`` (``1`` for doc-level).
        excerpt: A short snippet of the offending text (empty for doc-level).
        draft_rate: The draft's measured rate for the feature.
        user_rate: The user's measured rate, or ``None`` when unmeasured
            (the generic-prior fallback path).
        direction: ``over`` or ``under``.
        message: A human-readable explanation plus the deeper-problem hint.
    """

    tell_id: str
    category: Category
    feature_key: str
    span: Span
    line: int
    excerpt: str
    draft_rate: float
    user_rate: float | None
    direction: Direction
    message: str


@dataclass(frozen=True)
class ScanReport:
    """The result of scanning one body of text against a fingerprint.

    Attributes:
        findings: Every divergence that exceeded its margin.
        deltas: Per-``feature_key`` signed ``draft_rate - user_rate`` (the
            user side falling back to the generic prior when unmeasured).
        voice_distance: The aggregate weighted divergence. ``0.0`` means the
            draft's measured profile matches the user's.
        thin_fingerprint: ``True`` when the fingerprint was below the
            configured ``min_fingerprint_fragments`` and flagging was softened.
    """

    findings: list[Finding] = field(default_factory=list)
    deltas: dict[str, float] = field(default_factory=dict)
    voice_distance: float = 0.0
    thin_fingerprint: bool = False
