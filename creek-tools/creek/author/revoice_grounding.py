"""Owner-voice grounding for revoice: what makes a rewrite *this* owner's voice.

Generated voice skills reach the model as canonical signatures (#1986), which
are identical for every vault. On their own they describe a register, not a
person, and that is the root cause of a revoice that returned ``ok`` without
sounding like the owner. Grounding therefore comes from one of two sources:

* **Owner-authored style skills**, the trusted customization surface; or
* **The owner's register samples**, the exemplars ``creek voice`` saves
  under ``07-Voice/Register-Samples/``. Each is re-screened (self-authored,
  ``voice_weight > 0``, tier admitted at the caller's ceiling) by
  :func:`creek.generate.voice.load_owner_voice_samples`.

From the samples comes a numeric :class:`~creek.author.revoice_style.StyleVector`
profile. It is used in the prompt and by the quality gate, and it carries no
owner words. Short excerpts are added only on the one stronger retry, and only
from samples already admitted at the caller's ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from creek.author.revoice_check import Diagnostic
from creek.author.revoice_style import StyleVector, style_vector
from creek.generate.voice import load_owner_voice_samples

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.privacy_filter import PrivacyTierOverride

MIN_OWNER_SAMPLES: Final = 3
"""Fewest admitted samples from which an owner profile is measured."""
MIN_OWNER_WORDS: Final = 150
"""Fewest admitted sample words from which an owner profile is measured."""
MAX_RETRY_EXCERPTS: Final = 3
"""Owner excerpts added to the stronger retry prompt."""
MAX_EXCERPT_CHARS: Final = 600
"""Characters kept from each excerpt, cut back to a sentence end."""

RETRY_FEEDBACK: Final[dict[Diagnostic, str]] = {
    Diagnostic.OPENING_UNCHANGED: (
        "The opening was nearly a copy of the source. Rewrite the opening "
        "sentences in my voice."
    ),
    Diagnostic.SECTION_UNCHANGED: (
        "At least one section was left essentially unchanged. Rewrite every "
        "section, not only some."
    ),
    Diagnostic.COPYEDIT_ONLY: (
        "The attempt was a copyedit. Rebuild the sentences rather than polishing them."
    ),
    Diagnostic.NO_STYLE_SHIFT: (
        "The attempt read as generic prose. Match the owner style profile and "
        "the samples of my own writing."
    ),
    Diagnostic.HEADINGS_CHANGED: "A heading changed. Keep every heading exactly.",
    Diagnostic.CITATIONS_DROPPED: (
        "Citations, footnote references or links were removed. Keep every one "
        "exactly as written."
    ),
}
"""Fixed, content-free retry instructions keyed by the failed check."""


@dataclass(frozen=True)
class OwnerGrounding:
    """The owner evidence available to one revoice request.

    Attributes:
        profile: Measured owner style, or ``None`` with too few samples.
        excerpts: Retry excerpts, requested register first; empty without a
            profile.
        sample_count: Admitted samples, reported to the caller as a count.
    """

    profile: StyleVector | None
    excerpts: tuple[str, ...]
    sample_count: int


def _excerpt(body: str) -> str:
    """Return *body* flattened and cut back to a sentence end within bounds."""
    flat = " ".join(body.split())
    if len(flat) <= MAX_EXCERPT_CHARS:
        return flat
    head = flat[:MAX_EXCERPT_CHARS]
    end = max(head.rfind(". "), head.rfind("? "), head.rfind("! "))
    return head[: end + 1] if end > 0 else head


def owner_grounding(
    vault: Path, *, override: PrivacyTierOverride, register: str | None
) -> OwnerGrounding:
    """Measure the owner's admitted samples into a profile and retry excerpts.

    Args:
        vault: The vault root.
        override: The admission ceiling matching the caller's declared one.
        register: The requested register, whose samples lead the excerpts.

    Returns:
        The grounding; ``profile`` is ``None`` when samples are too thin.
    """
    samples = load_owner_voice_samples(vault, override=override)
    profile = style_vector("\n\n".join(sample.body for sample in samples))
    if len(samples) < MIN_OWNER_SAMPLES or profile.words < MIN_OWNER_WORDS:
        return OwnerGrounding(None, (), len(samples))
    ordered = sorted(samples, key=lambda sample: sample.register != register)
    excerpts = tuple(_excerpt(sample.body) for sample in ordered[:MAX_RETRY_EXCERPTS])
    return OwnerGrounding(profile, excerpts, len(samples))
