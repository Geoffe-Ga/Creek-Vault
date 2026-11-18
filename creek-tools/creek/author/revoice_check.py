"""Decide, inside Creek, whether a revoice actually applied the owner's voice.

A model that returns well-formed text has not necessarily changed its voice.
The failure this module exists for: an analytical essay revoiced in the
``analytical`` register came back with citations stripped, punctuation and
spelling touched and a few phrases compressed. The opening was almost
unchanged, but the call still reported ``ok``. A downstream client then
counted the raw textual difference as a successful transformation. Whether
Creek applied the voice is Creek's call to make, so the verdict lives here
rather than in each client's heuristics.

Two independent conditions must both hold:

* **Owner conditioning.** At least one applied voice skill must carry
  owner-authored style. A native generated skill is reconstructed as its
  canonical source-free signature (#1986). That signature is the same text
  for every vault, so it describes a register, not this owner.
* **Material rewrite.** Enough of the output's words must be new, measured
  after citations, links, footnotes, punctuation and case are normalized
  away. Only tokens inserted or substituted in the output count; deleting
  source text rewrites nothing, so compression alone cannot pass.

The verdict is deterministic and makes no model call. It measures how much of
the prose moved, not whether the result sounds like the owner. It is a floor
against no-op "rewrites" and does not certify authorship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any, Final

MIN_REWRITTEN_RATIO: Final = 0.25
"""Share of normalized output tokens that must be new for a rewrite to count.

Cosmetic cleanup (spelling, punctuation, a dropped citation, an occasional
synonym) stays well below this. A genuine re-voicing, where most sentences are
re-phrased, sits far above it.
"""

OPENING_WINDOW_TOKENS: Final = 50
"""How many leading normalized tokens the opening-similarity diagnostic compares."""

_FOOTNOTE_DEFINITION: Final = re.compile(r"^\[\^[^\]]+\]:.*$", re.MULTILINE)
_FOOTNOTE_REFERENCE: Final = re.compile(r"\[\^[^\]]+\]")
_NUMERIC_CITATION: Final = re.compile(r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\]")
_MARKDOWN_LINK: Final = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BARE_URL: Final = re.compile(r"https?://\S+")
_AUTHOR_YEAR: Final = re.compile(r"\([^()]*\b(?:1[5-9]|20)\d{2}[a-z]?\b[^()]*\)")
_WORD: Final = re.compile(r"[^\W_]+")


class VoiceConditioning(StrEnum):
    """What actually conditioned the rewrite's style."""

    OWNER = "owner"
    """At least one applied skill carries owner-authored style."""
    GENERIC = "generic"
    """Only canonical skills applied; they are identical for every owner."""
    NONE = "none"
    """No voice skill applied; the care policy alone conditioned the model."""


class UnvoicedReason(StrEnum):
    """A closed, content-free reason a rewrite does not count as voiced."""

    NO_OWNER_STYLE = "no_owner_style"
    INSUFFICIENT_REWRITE = "insufficient_rewrite"


@dataclass(frozen=True)
class RevoiceCheck:
    """Creek's verdict on one rewrite, with the measurements behind it.

    Attributes:
        conditioning: What conditioned the style.
        owner_style_skills: Applied skill names carrying owner-authored style.
        rewritten_ratio: Share of normalized output tokens that are new.
        opening_similarity: Similarity of the first normalized tokens, ``0..1``.
            This is a diagnostic only and does not decide the verdict.
        reasons: Every failed condition, in a stable order; empty on success.
    """

    conditioning: VoiceConditioning
    owner_style_skills: tuple[str, ...]
    rewritten_ratio: float
    opening_similarity: float
    reasons: tuple[UnvoicedReason, ...]

    @property
    def voice_applied(self) -> bool:
        """Whether Creek stands behind this as a rewrite in the owner's voice."""
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        """Project the verdict onto JSON-ready names, numbers and codes only."""
        return {
            "voice_applied": self.voice_applied,
            "conditioning": self.conditioning.value,
            "owner_style_skills": list(self.owner_style_skills),
            "rewritten_ratio": round(self.rewritten_ratio, 4),
            "min_rewritten_ratio": MIN_REWRITTEN_RATIO,
            "opening_similarity": round(self.opening_similarity, 4),
            "reasons": [reason.value for reason in self.reasons],
        }


def normalized_tokens(text: str) -> list[str]:
    """Return the lowercase words of *text* without citation or link apparatus.

    Footnote definitions and references, numeric and author-year citations,
    and URLs are removed. Markdown links keep their visible text. Punctuation
    and case are discarded.

    Args:
        text: Markdown prose.

    Returns:
        The prose's words, in order.
    """
    stripped = _FOOTNOTE_DEFINITION.sub(" ", text)
    for pattern in (_FOOTNOTE_REFERENCE, _NUMERIC_CITATION, _AUTHOR_YEAR):
        stripped = pattern.sub(" ", stripped)
    stripped = _MARKDOWN_LINK.sub(r"\1", stripped)
    stripped = _BARE_URL.sub(" ", stripped)
    return _WORD.findall(stripped.lower())


def _matcher(source: list[str], output: list[str]) -> SequenceMatcher[str]:
    """Build a token matcher; autojunk would hide common words in long essays."""
    return SequenceMatcher(None, source, output, autojunk=False)


def rewritten_ratio(source: str, output: str) -> float:
    """Return the share of *output*'s normalized tokens absent from *source*.

    Tokens are aligned in order, so moving a word does not hide a rewrite and
    deleting source text adds nothing. An empty output scores ``0.0``.

    Args:
        source: The passage the caller supplied.
        output: The model's rewrite.

    Returns:
        A ratio in ``[0, 1]``.
    """
    out = normalized_tokens(output)
    if not out:
        return 0.0
    matched = sum(
        block.size
        for block in _matcher(normalized_tokens(source), out).get_matching_blocks()
    )
    return (len(out) - matched) / len(out)


def _opening_similarity(source: str, output: str) -> float:
    """Return how alike the two openings are, over the first tokens of each."""
    head = slice(OPENING_WINDOW_TOKENS)
    return _matcher(
        normalized_tokens(source)[head], normalized_tokens(output)[head]
    ).ratio()


def _conditioning(
    owner_style_skills: tuple[str, ...], voice_skills: tuple[str, ...]
) -> VoiceConditioning:
    """Classify what conditioned the style from the applied skill names."""
    if owner_style_skills:
        return VoiceConditioning.OWNER
    return VoiceConditioning.GENERIC if voice_skills else VoiceConditioning.NONE


def check_revoice(
    source: str,
    output: str,
    *,
    owner_style_skills: tuple[str, ...],
    voice_skills: tuple[str, ...],
) -> RevoiceCheck:
    """Judge whether *output* is a rewrite of *source* in the owner's voice.

    Args:
        source: The passage the caller supplied.
        output: The model's rewrite.
        owner_style_skills: Applied skills that carry owner-authored style.
        voice_skills: Every applied skill name.

    Returns:
        The verdict, with its measurements and any failed conditions.
    """
    conditioning = _conditioning(owner_style_skills, voice_skills)
    ratio = rewritten_ratio(source, output)
    reasons: list[UnvoicedReason] = []
    if conditioning is not VoiceConditioning.OWNER:
        reasons.append(UnvoicedReason.NO_OWNER_STYLE)
    if ratio < MIN_REWRITTEN_RATIO:
        reasons.append(UnvoicedReason.INSUFFICIENT_REWRITE)
    return RevoiceCheck(
        conditioning=conditioning,
        owner_style_skills=owner_style_skills,
        rewritten_ratio=ratio,
        opening_similarity=_opening_similarity(source, output),
        reasons=tuple(reasons),
    )
