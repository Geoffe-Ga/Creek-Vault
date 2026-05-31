"""Voice-distance scoring: how far a draft sits from the user's voice.

The score is **vault-relative** (divergence from the user's measured
rates, not a generic baseline), **directional** (over-using an avoided
feature *and* under-using a signature feature both count), and **bounded**
(a saturating transform keeps any single feature from dominating, so one
stray word can never blow up the score). A feature where the draft matches
the user contributes exactly zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creek.generate.ai_style.tells import Polarity

_SATURATION_SCALE = 5.0
"""Rate (per 1000 words) at which a single feature's divergence reaches
half of its maximum contribution. Larger ⇒ more tolerant of divergence.
Fixed (not configurable) so the 0..1 distance scale stays comparable
across vaults; tune relative importance via per-feature/category weights."""


def bad_direction_magnitude(
    polarity: Polarity,
    draft_rate: float,
    user_rate: float,
) -> float:
    """Return the divergence magnitude in the *concerning* direction.

    For an ``avoid`` feature only over-use matters (draft above the user);
    for a ``signature`` feature only under-use matters (draft below the
    user). Divergence in the harmless direction returns ``0.0``.

    Args:
        polarity: The owning tell's polarity.
        draft_rate: The draft's measured rate.
        user_rate: The user's measured rate (or generic prior).

    Returns:
        A non-negative magnitude; ``0.0`` when the draft is on the safe
        side of, or equal to, the user's rate.
    """
    delta = draft_rate - user_rate
    if polarity == "avoid":
        return max(0.0, delta)
    return max(0.0, -delta)


def _saturate(magnitude: float) -> float:
    """Map a non-negative rate divergence into ``[0, 1)``.

    Args:
        magnitude: The bad-direction divergence (per 1000 words).

    Returns:
        ``magnitude / (magnitude + scale)`` — 0 at no divergence, asymptotic
        to 1 as divergence grows.
    """
    return magnitude / (magnitude + _SATURATION_SCALE)


@dataclass(frozen=True)
class FeatureContribution:
    """One feature's input to the aggregate voice distance.

    Attributes:
        feature_key: The diverging feature.
        weight: The resolved category/feature weight.
        magnitude: The bad-direction divergence (per 1000 words).
    """

    feature_key: str
    weight: float
    magnitude: float


def voice_distance(
    contributions: list[FeatureContribution],
    *,
    softening: float = 1.0,
) -> float:
    """Aggregate per-feature contributions into a scalar voice distance.

    The result is a weighted mean of saturated magnitudes, so it sits in
    ``[0, 1)``: ``0.0`` when every feature matches the user, rising toward
    ``1`` as divergences accumulate.

    Args:
        contributions: One entry per measured feature.
        softening: Multiplier in ``[0, 1]`` applied to the whole score when
            the fingerprint is thin (see ``AIStyleConfig`` —
            ``thin_fingerprint_softening``). ``1.0`` is no softening.

    Returns:
        The aggregate distance in ``[0, 1)``.
    """
    total_weight = sum(c.weight for c in contributions)
    if total_weight <= 0.0:
        return 0.0
    weighted = sum(c.weight * _saturate(c.magnitude) for c in contributions)
    return softening * weighted / total_weight
