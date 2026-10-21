"""Voice-distance scoring: how far a draft sits from the user's voice.

The score is **vault-relative** (divergence from the user's measured
rates, not a generic baseline), **directional** (over-using an avoided
feature *and* under-using a signature feature both count), and **bounded**
(a saturating transform keeps any single feature from dominating, so one
stray word can never blow up the score). A feature where the draft matches
the user contributes exactly zero.

**Aggregation: a union of per-family evidence, not a mean (#1936).** Until
#1936 the scalar was a weighted *mean* of saturated magnitudes over every
tell the scanner measured — 17 in an article scan, total weight 16.5 — so
the families that did **not** diverge sat in the denominator and diluted
the ones that did. Measured with stock
:class:`~creek.config.AIStyleConfig` defaults, against the eval harness's
16-fragment synthetic corpus for the two candidate fixtures and against
the ``tests/fixtures/voice/in_voice`` fingerprint for the synthetic
specimens (whose scores are the same either way: both voices measure zero
on every family these specimens trip):

| text                                            | words | old    | new    |
|-------------------------------------------------|-------|--------|--------|
| a corpus entry / the on-voice candidate fixture | 56-84 | 0.0000 | 0.0000 |
| on-voice plus one AI-vocabulary word            |    88 | 0.0421 | 0.6944 |
| the fixture written deliberately off-voice      |    58 | 0.1012 | 0.9778 |
| a synthetic specimen tripping 1 family          |    53 | 0.0535 | 0.8830 |
| ditto, 2 families                               |    62 | 0.0987 | 0.9682 |
| ditto, 6 families                               |    87 | 0.2685 | 0.9997 |
| ditto, all 16 active families                   |   162 | 0.5101 | 1.0000 |

With ``voice_distance_upper`` at 0.35 the old column first flags at **nine**
simultaneous families (0.3698; eight scores 0.3354) — a lab specimen, not
prose anyone writes — so ``creek lint``'s voice-fidelity check reported a
clean draft for text that fired six findings. One tripped family in a 60-90
word draft contributed about 0.042.

The fix is structural rather than a recalibrated ceiling, which would have
separated today's fixtures while leaving the dilution (and so would
re-break as soon as a draft got longer or a tell family was added):

* **Each family's evidence is gated at its own margin.** Below the margin a
  divergence is tolerated noise and the family contributes *nothing*; above
  it the family contributes its saturated magnitude. The margin was already
  the line at which the scanner emits a
  :class:`~creek.generate.ai_style.model.Finding`, so the scalar and the
  findings now agree by construction: ``voice_distance == 0.0`` exactly when
  a scan produced no finding. Prose that fires nothing scores exactly zero,
  which is why a far stricter score costs no false positive on the user's
  own writing.
* **The families combine as independent evidence** (a noisy-OR / union),
  not as an average: ``1 - Π(1 - wᵢ · eᵢ)``. A clean family multiplies by
  exactly 1, so it cannot dilute; the worst family sets a floor the rest
  cannot average away; breadth still accumulates, since every additional
  diverging family strictly raises the score.

No new tuning knob was added: the per-tell margins, the category/feature
weights and :data:`SATURATION_SCALE` are all pre-existing, and weights
still matter only in ratio (each is taken relative to the heaviest family
in play, where the mean divided by their sum). The flag boundary is
therefore arithmetic rather than taste: one weight-1.0 family clears a
ceiling ``c`` when its rate exceeds ``SATURATION_SCALE · c / (1 - c)`` —
2.69 per 1000 words at the shipped ``c = 0.35``, i.e. about one occurrence
per 371 words. The score stays a **density**, so a draft is never punished
for being long: the same text at 10x length scores identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creek.generate.ai_style.tells import Polarity

SATURATION_SCALE = 5.0
"""Rate (per 1000 words) at which a single feature's divergence reaches
half of its maximum contribution. Larger ⇒ more tolerant of divergence.
Fixed (not configurable) so the 0..1 distance scale stays comparable
across vaults; tune relative importance via per-feature/category weights.
Public since #1936 so callers and tests can *derive* the flag boundary from
it (see the module docstring) instead of restating a magic number."""


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
    return magnitude / (magnitude + SATURATION_SCALE)


@dataclass(frozen=True)
class FeatureContribution:
    """One feature's input to the aggregate voice distance.

    Attributes:
        feature_key: The diverging feature.
        weight: The resolved category/feature weight.
        magnitude: The bad-direction divergence (per 1000 words).
        margin: The divergence this feature tolerates before it counts as
            evidence at all — the same per-tell/config margin at which the
            scanner emits a finding. Deliberately required rather than
            defaulted: since #1936 the margin is load-bearing in the score,
            and a silent default would let a mis-wired caller score a
            tolerated divergence as a real one.
    """

    feature_key: str
    weight: float
    magnitude: float
    margin: float


def evidence_for(contribution: FeatureContribution) -> float:
    """Return how strongly *contribution* evidences a real divergence.

    Zero at or below the feature's margin — the band the tell itself treats
    as ordinary variation, where it emits no finding — and the saturated
    magnitude above it. So a family either counts or it does not, and the
    scalar can never disagree with the findings about which.

    Args:
        contribution: One measured feature's divergence and margin.

    Returns:
        Evidence in ``[0, 1)``; ``0.0`` for a feature inside its margin.
    """
    if contribution.magnitude <= contribution.margin:
        return 0.0
    return _saturate(contribution.magnitude)


def voice_distance(
    contributions: list[FeatureContribution],
    *,
    softening: float = 1.0,
) -> float:
    """Aggregate per-feature contributions into a scalar voice distance.

    The union (noisy-OR) of each family's :func:`evidence_for`, weighted by
    its weight *relative to the heaviest family in play* — so the result
    sits in ``[0, 1]``, is ``0.0`` exactly when no family cleared its
    margin, rises with both the severity of the worst family and the number
    of diverging families, and is unchanged by families that match the
    user. See the module docstring for why this replaced a weighted mean,
    and for the measurements behind it (#1936).

    Args:
        contributions: One entry per measured feature.
        softening: Multiplier in ``[0, 1]`` applied to the whole score when
            the fingerprint is thin (see ``AIStyleConfig`` —
            ``thin_fingerprint_softening``). ``1.0`` is no softening.

    Returns:
        The aggregate distance in ``[0, 1]``. Each family's evidence is
        strictly below 1, but a union of enough of them underflows the
        product to zero in float64 and reaches exactly 1.0 — which takes a
        physically impossible rate, not a real draft.
    """
    weighted = [c for c in contributions if c.weight > 0.0]
    if not weighted:
        return 0.0
    heaviest = max(c.weight for c in weighted)
    agreement = 1.0
    for contribution in weighted:
        relative = contribution.weight / heaviest
        agreement *= 1.0 - relative * evidence_for(contribution)
    return softening * (1.0 - agreement)
