"""Deterministic prose measurements shared by revoice grounding and its gate.

Two consumers need the same view of a passage:

* the **owner profile** (:class:`StyleVector` over owner-authored samples),
  rendered into the prompt as numeric, source-free style guidance; and
* the **quality gate** (:mod:`creek.author.revoice_check`), which compares
  source, rewrite and owner on those same axes, sentence by sentence and
  section by section.

Measurements run on *prose*: headings, citations, footnotes and link targets
are scholarly apparatus that a revoice must preserve. They say nothing about
the owner's voice, so they are removed first.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

_FOOTNOTE_DEFINITION: Final = re.compile(r"^\[\^[^\]]+\]:.*$", re.MULTILINE)
_FOOTNOTE_REFERENCE: Final = re.compile(r"\[\^[^\]]+\]")
_NUMERIC_CITATION: Final = re.compile(r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\]")
_AUTHOR_YEAR: Final = re.compile(r"\([^()]*\b(?:1[5-9]|20)\d{2}[a-z]?\b[^()]*\)")
_MARKDOWN_LINK: Final = re.compile(r"(!?)\[([^\]]*)\]\(([^)]*)\)")
_BARE_URL: Final = re.compile(r"https?://[^\s)\]>]+")
_HEADING: Final = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])[\"'\u201d\u2019)]*\s+")
_SPACE_BEFORE_PUNCTUATION: Final = re.compile(r"[ \t]+([.,;:!?])")
_WORD: Final = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")
_EM_DASH: Final = re.compile(r"\u2014|(?<!-)--(?!-)|\s\u2013\s")
_PARENTHETICAL: Final = re.compile(r"\([^()]{3,}\)")
_FIRST_PERSON: Final = frozenset({"i", "me", "my", "mine", "myself"})
_PER_THOUSAND: Final = 1000.0
_SHORT_SENTENCE_WORDS: Final = 9


def citation_markers(text: str) -> frozenset[str]:
    """Return every citation, footnote reference and link target in *text*.

    These are the apparatus a revoice must carry through unchanged. They are
    compared as whitespace-normalized strings, so reflowing a line is not a
    loss.

    Args:
        text: Markdown prose.

    Returns:
        The distinct markers.
    """
    found: set[str] = set()
    for pattern in (_FOOTNOTE_REFERENCE, _NUMERIC_CITATION, _AUTHOR_YEAR):
        found.update(" ".join(match.split()) for match in pattern.findall(text))
    found.update(
        target.strip() for _bang, _label, target in _MARKDOWN_LINK.findall(text)
    )
    found.update(_BARE_URL.findall(_MARKDOWN_LINK.sub(" ", text)))
    return frozenset(marker for marker in found if marker)


def headings(text: str) -> tuple[str, ...]:
    """Return the Markdown heading texts of *text*, in order, case-folded."""
    return tuple(" ".join(match.split()).casefold() for match in _HEADING.findall(text))


def prose(text: str) -> str:
    """Return *text* without headings, citations, footnotes or link targets."""
    stripped = _FOOTNOTE_DEFINITION.sub(" ", text)
    stripped = _HEADING.sub(" ", stripped)
    for pattern in (_FOOTNOTE_REFERENCE, _NUMERIC_CITATION, _AUTHOR_YEAR):
        stripped = pattern.sub(" ", stripped)
    stripped = _MARKDOWN_LINK.sub(r"\2", stripped)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", _BARE_URL.sub(" ", stripped))


def tokens(text: str) -> list[str]:
    """Return the lowercase words of *text*'s prose, apostrophes folded in."""
    return [word.replace("\u2019", "'") for word in _WORD.findall(prose(text).lower())]


def sentences(text: str) -> list[str]:
    """Split *text*'s prose into sentences on terminal punctuation."""
    flat = " ".join(prose(text).split())
    return [part for part in _SENTENCE_END.split(flat) if _WORD.search(part)]


def split_sections(text: str) -> list[str]:
    """Split *text* at Markdown headings; the opening is section zero.

    Args:
        text: Markdown prose.

    Returns:
        The opening (possibly empty) followed by one entry per heading, each
        starting with its heading line.
    """
    starts = [match.start() for match in _HEADING.finditer(text)]
    bounds = [0, *starts, len(text)]
    return [text[a:b] for a, b in pairwise(bounds)]


@dataclass(frozen=True)
class StyleVector:
    """Cadence, sentence construction and habit rates of one passage.

    Attributes:
        mean_sentence_words: Mean words per sentence.
        sentence_length_spread: Coefficient of variation of sentence length;
            high means the writer mixes short and long sentences.
        short_sentence_share: Share of sentences of nine words or fewer.
        question_share: Share of sentences ending in a question mark.
        em_dashes_per_k: Em-dashes per thousand words.
        parentheticals_per_k: Parenthetical asides per thousand words.
        first_person_per_k: First-person singular pronouns per thousand words.
        contractions_per_k: Contractions per thousand words.
        words: Words measured; small samples are unreliable.
    """

    mean_sentence_words: float
    sentence_length_spread: float
    short_sentence_share: float
    question_share: float
    em_dashes_per_k: float
    parentheticals_per_k: float
    first_person_per_k: float
    contractions_per_k: float
    words: int


_SCALES: Final[dict[str, float]] = {
    "mean_sentence_words": 6.0,
    "sentence_length_spread": 0.3,
    "short_sentence_share": 0.2,
    "question_share": 0.1,
    "em_dashes_per_k": 5.0,
    "parentheticals_per_k": 5.0,
    "first_person_per_k": 15.0,
    "contractions_per_k": 10.0,
}
"""Difference on each axis that counts as one unit of style distance."""

_MAX_AXIS_DISTANCE: Final = 3.0
"""Cap per axis, so one extreme habit cannot dominate the distance."""


def _per_k(count: int, words: int) -> float:
    """Return *count* per thousand *words*, or zero for an empty passage."""
    return count / words * _PER_THOUSAND if words else 0.0


def _length_stats(lengths: list[int]) -> tuple[float, float, float]:
    """Return mean, coefficient of variation and short share of *lengths*."""
    if not lengths:
        return 0.0, 0.0, 0.0
    mean = sum(lengths) / len(lengths)
    variance = sum((n - mean) ** 2 for n in lengths) / len(lengths)
    spread = math.sqrt(variance) / mean if mean else 0.0
    short = sum(1 for n in lengths if n <= _SHORT_SENTENCE_WORDS) / len(lengths)
    return mean, spread, short


def style_vector(text: str) -> StyleVector:
    """Measure *text*'s prose on every :class:`StyleVector` axis."""
    body = prose(text)
    words = tokens(text)
    parts = sentences(text)
    mean, spread, short = _length_stats([len(_WORD.findall(s)) for s in parts])
    count = len(words)
    return StyleVector(
        mean_sentence_words=mean,
        sentence_length_spread=spread,
        short_sentence_share=short,
        question_share=(
            sum(1 for s in parts if s.rstrip("\"'\u201d\u2019) ").endswith("?"))
            / len(parts)
            if parts
            else 0.0
        ),
        em_dashes_per_k=_per_k(len(_EM_DASH.findall(body)), count),
        parentheticals_per_k=_per_k(len(_PARENTHETICAL.findall(body)), count),
        first_person_per_k=_per_k(sum(w in _FIRST_PERSON for w in words), count),
        contractions_per_k=_per_k(sum("'" in w for w in words), count),
        words=count,
    )


def style_distance(a: StyleVector, b: StyleVector) -> float:
    """Return the mean scaled, capped per-axis difference between two passages."""
    gaps = [
        min(
            abs(float(getattr(a, axis)) - float(getattr(b, axis))) / scale,
            _MAX_AXIS_DISTANCE,
        )
        for axis, scale in _SCALES.items()
    ]
    return sum(gaps) / len(gaps)


def render_profile(profile: StyleVector) -> str:
    """Render an owner profile as numeric, source-free style guidance.

    No owner words, titles or ids appear; only rates derived from the owner's
    admitted samples.

    Args:
        profile: The owner's measured style.

    Returns:
        A Markdown block for the dynamic prompt.
    """
    return "\n".join(
        [
            "## Owner style profile (measured from my own writing)",
            f"- Sentences average {profile.mean_sentence_words:.0f} words; "
            f"{profile.short_sentence_share:.0%} are nine words or fewer, and "
            f"lengths vary by {profile.sentence_length_spread:.0%} around the mean.",
            f"- {profile.question_share:.0%} of sentences are questions.",
            f"- Per thousand words: {profile.first_person_per_k:.0f} first-person "
            f"pronouns, {profile.contractions_per_k:.0f} contractions, "
            f"{profile.em_dashes_per_k:.0f} em-dashes, "
            f"{profile.parentheticals_per_k:.0f} parenthetical asides.",
            "- Match this cadence, sentence construction and these habits. "
            "Do not copy the passage's sentence shapes.",
        ]
    )
