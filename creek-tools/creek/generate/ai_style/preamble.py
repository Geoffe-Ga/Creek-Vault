"""Idiolect-driven prompt prevention (FEAT-040.8): steer toward *this* voice.

Prevention is the primary lever — cheaper and more faithful than repairing
AI-voice after generation. The preamble is **data-driven from the voice
fingerprint**: it states the user's measured habits as positive targets and
renders the registry avoid-list *minus the features the user genuinely uses*.
That is what turns "not like AI" into "like this writer": you never tell a
writer who genuinely loves em-dashes or "tapestry" to avoid them.

The preamble has three parts, all derived from the fingerprint + registry so
it tracks both the catalog and the specific user (never hand-copied):

1. **Measured targets** — a curated set of fingerprint features whose
   measured rate reads as a voice signature, rendered with the user's own
   number ("uses em-dashes freely (~4.2 per 1000 words)").
2. **Keep-these** — the avoid-registry tells the user *genuinely uses*
   (measured rate well above the generic prior), surfaced as their own voice
   rather than something to strip.
3. **Avoids** — the remaining avoid tells, deduplicated and length-capped.

When the fingerprint is thin (below ``min_fingerprint_fragments``) the
measured numbers are untrustworthy, so the preamble falls back to the generic
avoid-list with a "preliminary" note rather than fabricating targets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from creek.generate.ai_style.distance import bad_direction_magnitude
from creek.generate.ai_style.tells import TELL_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Iterable

    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint
    from creek.generate.ai_style.tells import Tell


class _MeasuredTarget(NamedTuple):
    """A fingerprint feature framed as a positive, rate-filled voice target.

    Attributes:
        feature_key: The fingerprint key supplying the user's measured rate.
        noun: The human noun phrase for the feature (e.g. ``"em-dashes"``).
        pivot: Rate (per 1000 words) separating the *high* and *low* framings.
        high: Adverb used when the measured rate is at or above ``pivot``.
        low: Adverb used when the measured rate is below ``pivot``.
    """

    feature_key: str
    noun: str
    pivot: float
    high: str
    low: str


# Curated positive targets: fingerprint features whose measured rate reads as
# a voice signature worth matching. The phrasing is generic; the user's own
# measured rate is filled in, so the target tracks THIS writer, not a
# template. Features the user barely uses simply render with the low adverb.
_MEASURED_TARGETS: tuple[_MeasuredTarget, ...] = (
    _MeasuredTarget("em_dash_density", "em-dashes", 1.0, "freely", "sparingly"),
    _MeasuredTarget(
        "concrete_density",
        "concrete, specific wording",
        1.0,
        "heavily",
        "lightly",
    ),
)

_HEADER = "## Voice targets"
_TARGETS_LEAD = "This writer's voice, measured from their own work — write toward it:"
_KEEP_LEAD = (
    "These read as AI-ish in general but are genuinely this writer's — keep them:"
)
_AVOID_LEAD = "Avoid drifting toward these AI tells (the writer does not use them):"
_THIN_NOTE = (
    "Voice profile is preliminary (thin corpus); the avoids below are generic "
    "AI-avoidance, not yet personalised to this writer."
)


def build_style_preamble(
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> str:
    """Return the ``## Voice targets`` prompt preamble for *fingerprint*.

    Args:
        fingerprint: The user's measured voice fingerprint.
        config: The AI-style config (toggles, margins, and length cap).

    Returns:
        The assembled preamble section, or ``""`` when prompt prevention is
        disabled.
    """
    if not config.prevent_in_prompt:
        return ""
    thin = fingerprint.is_thin(config.min_fingerprint_fragments)
    avoid_tells = [t for t in TELL_REGISTRY.values() if t.polarity == "avoid"]
    genuine = set() if thin else _genuine_ids(avoid_tells, fingerprint, config)
    blocks = _fixed_blocks(fingerprint, config, avoid_tells, genuine, thin=thin)
    avoid_lines = _dedupe(t.description for t in avoid_tells if t.id not in genuine)
    return _assemble(blocks, avoid_lines, config.preamble_max_chars)


def _user_genuinely_uses(
    tell: Tell,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> bool:
    """Return whether the user genuinely uses *tell*'s feature.

    A feature is "genuinely theirs" when the user's measured rate sits more
    than the tell's margin above its generic prior — the same bad-direction
    comparison the scanner uses, but applied to the user instead of a draft.
    A never-legitimate tell (margin ``0``) is never genuine, so placeholder
    text and knowledge-cutoff disclaimers can never be excused as voice.

    Args:
        tell: The avoid tell under consideration.
        fingerprint: The user's measured fingerprint.
        config: The AI-style config (for the default margin).

    Returns:
        ``True`` when the user's own rate clears the margin above the prior.
    """
    margin = config.default_margin if tell.margin is None else tell.margin
    if margin <= 0.0:
        return False
    user_rate = fingerprint.rate_for(tell.feature_key)
    if user_rate is None:
        return False
    excess = bad_direction_magnitude(tell.polarity, user_rate, tell.generic_prior)
    return excess > margin


def _genuine_ids(
    avoid_tells: list[Tell],
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> set[str]:
    """Return the ids of avoid tells the user genuinely uses."""
    return {t.id for t in avoid_tells if _user_genuinely_uses(t, fingerprint, config)}


def _measured_target_lines(
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> list[str]:
    """Render the curated measured targets backed by sufficient support."""
    lines: list[str] = []
    minimum = config.min_fingerprint_fragments
    for target in _MEASURED_TARGETS:
        if fingerprint.support_for(target.feature_key) < minimum:
            continue
        rate = fingerprint.rate_for(target.feature_key)
        if rate is None:
            continue
        adverb = target.high if rate >= target.pivot else target.low
        lines.append(f"uses {target.noun} {adverb} (~{rate:.1f} per 1000 words)")
    return lines


def _fixed_blocks(
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
    avoid_tells: list[Tell],
    genuine: set[str],
    *,
    thin: bool,
) -> list[str]:
    """Return the always-kept blocks (targets / keep / thin note) above avoids."""
    if thin:
        return [_THIN_NOTE]
    if not config.include_measured_targets:
        return []
    blocks: list[str] = []
    targets = _measured_target_lines(fingerprint, config)
    if targets:
        blocks.append(f"{_TARGETS_LEAD}\n{_bullets(targets)}")
    kept = _dedupe(t.description for t in avoid_tells if t.id in genuine)
    if kept:
        blocks.append(f"{_KEEP_LEAD}\n{_bullets(kept)}")
    return blocks


def _assemble(blocks: list[str], avoid_lines: list[str], cap: int) -> str:
    """Assemble the header, fixed blocks, and as many avoids as fit the cap."""
    head = "\n\n".join([_HEADER, *blocks])
    if not avoid_lines:
        return head
    kept = _fit_avoids(head, avoid_lines, cap)
    if not kept:
        return head
    return f"{head}\n\n{_AVOID_LEAD}\n{_bullets(kept)}"


def _fit_avoids(head: str, avoid_lines: list[str], cap: int) -> list[str]:
    """Return the prefix of *avoid_lines* whose rendering stays within *cap*.

    Args:
        head: The already-assembled header + fixed blocks.
        avoid_lines: The candidate avoid descriptions, in priority order.
        cap: The character cap; ``0`` (or less) means unlimited.

    Returns:
        The avoid lines that fit under the cap (all of them when uncapped).
    """
    kept: list[str] = []
    for line in avoid_lines:
        candidate = f"{head}\n\n{_AVOID_LEAD}\n{_bullets([*kept, line])}"
        if cap > 0 and len(candidate) > cap:
            break
        kept.append(line)
    return kept


def _bullets(lines: Iterable[str]) -> str:
    """Render *lines* as a Markdown bullet list."""
    return "\n".join(f"- {line}" for line in lines)


def _dedupe(items: Iterable[str]) -> list[str]:
    """Return *items* with duplicates removed, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
