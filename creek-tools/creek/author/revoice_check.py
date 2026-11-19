"""Decide, inside Creek, whether a revoice actually applied the owner's voice.

A model that returns well-formed text has not necessarily changed its voice.
The failure this gate exists for: an analytical essay revoiced in the
``analytical`` register came back with a near-identical opening. Its edits
were citation removal, punctuation, spelling, compression and a few
substitutions, plus one generically rewritten stretch. The call reported
``ok``. A client then read the large aggregate diff as a successful voice
transformation. Whether the voice was applied is Creek's call, so the verdict
lives here rather than in client heuristics.

A raw diff cannot make that call: deleting citations, compressing, or one
heavily rewritten section all produce a big number. The gate combines four
checks, each over *prose* (headings, citations and link targets removed; see
:mod:`creek.author.revoice_style`):

* **Fidelity.** Every heading and every citation, footnote reference and link
  target in the source must survive. Removing apparatus is a defect, not a
  voice.
* **Per-section transformation.** Each substantial section must have new
  wording (``rewritten_ratio``) *and* rebuilt sentences
  (``copied_sentence_ratio``, the share of output sentences that are
  near-copies of a source sentence). Copyediting keeps the sentences.
  Compression only deletes, and deletions never count as new wording. A
  strong section cannot hide an unchanged one, because every section is
  judged on its own.
* **Opening.** The first words of the passage must not be a near-copy.
* **Style shift.** Given the owner's measured profile, the rewrite must sit
  closer to it than the source did, or already be close. Fluent generic
  model prose moves away from it.

The verdict is deterministic, makes no model call and contains only numbers
and closed codes, never prose.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from creek.author.revoice_style import (
    citation_markers,
    headings,
    sentences,
    split_sections,
    style_distance,
    style_vector,
    tokens,
)

if TYPE_CHECKING:
    from creek.author.revoice_style import StyleVector

MIN_REWRITTEN_RATIO: Final = 0.25
"""Share of a section's output words that must be new, not carried over."""
MAX_COPIED_SENTENCE_RATIO: Final = 0.5
"""Largest share of a section's output sentences that may be near-copies."""
COPIED_SENTENCE_SIMILARITY: Final = 0.75
"""Token similarity at which an output sentence counts as a source copy."""
MIN_SECTION_WORDS: Final = 40
"""Sections shorter than this are too small to judge on their own."""
OPENING_WORDS: Final = 40
"""How many leading prose words form the opening."""
MAX_OPENING_SIMILARITY: Final = 0.8
"""Opening similarity at or above which the opening is unchanged."""
MIN_STYLE_WORDS: Final = 80
"""Below this many output words, the style shift is not measured."""
STYLE_CLOSE_ENOUGH: Final = 0.5
"""Style distance at which a rewrite already sits close to the owner."""
MIN_STYLE_IMPROVEMENT: Final = 0.1
"""Relative distance reduction toward the owner that counts as a shift."""


class RevoiceReason(StrEnum):
    """The closed, top-level reason a revoice is refused."""

    NO_OWNER_VOICE = "no_owner_voice"
    """No owner-authored style or admitted owner samples could ground it."""
    INSUFFICIENT_TRANSFORMATION = "insufficient_transformation"
    """The rewrite did not substantively change voice."""
    FIDELITY_VIOLATION = "fidelity_violation"
    """The rewrite lost headings or citation apparatus."""


class Diagnostic(StrEnum):
    """A closed, content-free finding behind a refusal."""

    OPENING_UNCHANGED = "opening_unchanged"
    SECTION_UNCHANGED = "section_unchanged"
    COPYEDIT_ONLY = "copyedit_only"
    NO_STYLE_SHIFT = "no_style_shift"
    HEADINGS_CHANGED = "headings_changed"
    CITATIONS_DROPPED = "citations_dropped"


_TRANSFORMATION_FINDINGS: Final = frozenset(
    {
        Diagnostic.OPENING_UNCHANGED,
        Diagnostic.SECTION_UNCHANGED,
        Diagnostic.COPYEDIT_ONLY,
        Diagnostic.NO_STYLE_SHIFT,
    }
)


@dataclass(frozen=True)
class SectionCheck:
    """One heading-delimited section's transformation measurements.

    Attributes:
        index: Position in the passage; ``0`` is the text before any heading.
        source_words: Prose words in the source section.
        rewritten_ratio: Share of output words not carried over from source.
        copied_sentence_ratio: Share of output sentences that are near-copies.
        judged: Whether the section was large enough to judge.
    """

    index: int
    source_words: int
    rewritten_ratio: float
    copied_sentence_ratio: float
    judged: bool

    @property
    def unchanged(self) -> bool:
        """Whether this judged section is copyedited rather than revoiced."""
        return self.judged and (
            self.rewritten_ratio < MIN_REWRITTEN_RATIO
            or self.copied_sentence_ratio > MAX_COPIED_SENTENCE_RATIO
        )


@dataclass(frozen=True)
class RevoiceCheck:
    """Creek's verdict on one rewrite, with the measurements behind it.

    Attributes:
        sections: Per-section measurements, in passage order.
        rewritten_ratio: Whole-passage share of new output words.
        copied_sentence_ratio: Whole-passage share of near-copied sentences.
        opening_similarity: Similarity of the two openings, ``0..1``.
        style_distance_source: Source-to-owner style distance, if measured.
        style_distance_output: Output-to-owner style distance, if measured.
        diagnostics: Every finding, in a stable order; empty on success.
    """

    sections: tuple[SectionCheck, ...]
    rewritten_ratio: float
    copied_sentence_ratio: float
    opening_similarity: float
    style_distance_source: float | None
    style_distance_output: float | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def reason(self) -> RevoiceReason | None:
        """The top-level refusal reason, or ``None`` when the rewrite passes.

        A transformation failure outranks a fidelity one. The caller asked
        for a voice, and a copyedit that also lost citations has not
        delivered it.
        """
        if _TRANSFORMATION_FINDINGS.intersection(self.diagnostics):
            return RevoiceReason.INSUFFICIENT_TRANSFORMATION
        if self.diagnostics:
            return RevoiceReason.FIDELITY_VIOLATION
        return None

    def as_dict(self) -> dict[str, Any]:
        """Project the verdict onto JSON-ready numbers and closed codes only."""
        return {
            "voice_applied": self.reason is None,
            "diagnostics": [finding.value for finding in self.diagnostics],
            "rewritten_ratio": _round(self.rewritten_ratio),
            "copied_sentence_ratio": _round(self.copied_sentence_ratio),
            "opening_similarity": _round(self.opening_similarity),
            "unchanged_sections": [s.index for s in self.sections if s.unchanged],
            "style_distance_source": _round(self.style_distance_source),
            "style_distance_output": _round(self.style_distance_output),
        }


def _round(value: float | None) -> float | None:
    """Round a measurement for the wire, keeping ``None`` as unmeasured."""
    return None if value is None else round(value, 4)


def _matcher(a: list[str], b: list[str]) -> SequenceMatcher[str]:
    """Build a token matcher; autojunk would hide common words in long essays."""
    return SequenceMatcher(None, a, b, autojunk=False)


def rewritten_ratio(source: str, output: str) -> float:
    """Return the share of *output*'s prose words not carried over from *source*.

    Words are aligned in order. Deleting source text adds nothing, so
    compression scores zero. An empty output scores ``0.0``.
    """
    out = tokens(output)
    if not out:
        return 0.0
    blocks = _matcher(tokens(source), out).get_matching_blocks()
    return (len(out) - sum(block.size for block in blocks)) / len(out)


def _is_copy(sentence: list[str], candidates: list[list[str]]) -> bool:
    """Return whether *sentence* is a near-copy of any candidate sentence."""
    for candidate in candidates:
        matcher = _matcher(candidate, sentence)
        if (
            matcher.real_quick_ratio() >= COPIED_SENTENCE_SIMILARITY
            and matcher.ratio() >= COPIED_SENTENCE_SIMILARITY
        ):
            return True
    return False


def copied_sentence_ratio(source: str, output: str) -> float:
    """Return the share of *output*'s sentences that near-copy a source sentence."""
    out = [tokens(s) for s in sentences(output)]
    out = [s for s in out if s]
    if not out:
        return 0.0
    candidates = [t for t in (tokens(s) for s in sentences(source)) if t]
    return sum(_is_copy(s, candidates) for s in out) / len(out)


def opening_similarity(source: str, output: str) -> float:
    """Return how alike the first :data:`OPENING_WORDS` prose words are."""
    head = slice(OPENING_WORDS)
    return _matcher(tokens(source)[head], tokens(output)[head]).ratio()


def _section(index: int, source: str, output: str) -> SectionCheck:
    """Measure one aligned section pair."""
    words = len(tokens(source))
    return SectionCheck(
        index=index,
        source_words=words,
        rewritten_ratio=rewritten_ratio(source, output),
        copied_sentence_ratio=copied_sentence_ratio(source, output),
        judged=words >= MIN_SECTION_WORDS,
    )


def _fidelity(source: str, output: str) -> list[Diagnostic]:
    """Return the apparatus-preservation findings."""
    findings: list[Diagnostic] = []
    if headings(source) != headings(output):
        findings.append(Diagnostic.HEADINGS_CHANGED)
    if not citation_markers(source) <= citation_markers(output):
        findings.append(Diagnostic.CITATIONS_DROPPED)
    return findings


def _sections(source: str, output: str) -> tuple[SectionCheck, ...]:
    """Measure aligned sections, or the whole passage when headings moved."""
    pairs = list(zip(split_sections(source), split_sections(output), strict=False))
    if headings(source) != headings(output):
        pairs = [(source, output)]
    return tuple(_section(i, a, b) for i, (a, b) in enumerate(pairs))


def _style(
    source: str, output: str, owner: StyleVector | None
) -> tuple[float | None, float | None]:
    """Return source and output distances to the owner, when measurable."""
    out = style_vector(output)
    if owner is None or out.words < MIN_STYLE_WORDS:
        return None, None
    return style_distance(style_vector(source), owner), style_distance(out, owner)


def _shifted(before: float | None, after: float | None) -> bool:
    """Whether the output moved toward the owner, or already sits close."""
    if before is None or after is None:
        return True
    return after <= STYLE_CLOSE_ENOUGH or after <= before * (1 - MIN_STYLE_IMPROVEMENT)


def _transformation(
    sections: tuple[SectionCheck, ...],
    whole: tuple[float, float],
    opening: float,
    shifted: bool,
) -> list[Diagnostic]:
    """Return the voice-transformation findings."""
    findings: list[Diagnostic] = []
    if opening >= MAX_OPENING_SIMILARITY:
        findings.append(Diagnostic.OPENING_UNCHANGED)
    if any(section.unchanged for section in sections):
        findings.append(Diagnostic.SECTION_UNCHANGED)
    ratio, copied = whole
    if ratio < MIN_REWRITTEN_RATIO or copied > MAX_COPIED_SENTENCE_RATIO:
        findings.append(Diagnostic.COPYEDIT_ONLY)
    if not shifted:
        findings.append(Diagnostic.NO_STYLE_SHIFT)
    return findings


def check_revoice(
    source: str, output: str, *, owner: StyleVector | None
) -> RevoiceCheck:
    """Judge whether *output* is a faithful rewrite of *source* in the owner's voice.

    Args:
        source: The passage the caller supplied.
        output: The model's rewrite.
        owner: The owner's measured style, or ``None`` when grounding came
            from owner-authored skills alone; the style shift is then not
            measured.

    Returns:
        The verdict, with every finding and the measurements behind it.
    """
    sections = _sections(source, output)
    whole = (rewritten_ratio(source, output), copied_sentence_ratio(source, output))
    opening = opening_similarity(source, output)
    before, after = _style(source, output, owner)
    findings = _transformation(sections, whole, opening, _shifted(before, after))
    findings += _fidelity(source, output)
    return RevoiceCheck(
        sections=sections,
        rewritten_ratio=whole[0],
        copied_sentence_ratio=whole[1],
        opening_similarity=opening,
        style_distance_source=before,
        style_distance_output=after,
        diagnostics=tuple(findings),
    )


def main(argv: list[str] | None = None) -> int:
    """Judge a local source/rewrite pair exactly as ``creek.revoice`` would.

    Usage: ``python -m creek.author.revoice_check SOURCE OUTPUT [--vault V]
    [--ceiling open|personal|intimate|all] [--register R]``. With ``--vault``,
    the owner's register samples admitted at the ceiling supply the style
    profile. Prints the content-free verdict as JSON.

    Args:
        argv: Arguments after the program name; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` when the rewrite passes, ``1`` when it would be refused.
    """
    from creek.author.revoice_grounding import (
        owner_grounding,  # grounding imports this module
    )
    from creek.classify.privacy_filter import (
        PrivacyTierOverride,
    )

    parser = argparse.ArgumentParser(prog="python -m creek.author.revoice_check")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vault", type=Path)
    parser.add_argument(
        "--ceiling",
        choices=[override.value for override in PrivacyTierOverride],
        default=PrivacyTierOverride.OPEN.value,
    )
    parser.add_argument("--register")
    args = parser.parse_args(argv)
    owner = (
        owner_grounding(
            args.vault,
            override=PrivacyTierOverride(args.ceiling),
            register=args.register,
        ).profile
        if args.vault is not None
        else None
    )
    check = check_revoice(
        args.source.read_text(encoding="utf-8"),
        args.output.read_text(encoding="utf-8"),
        owner=owner,
    )
    reason = check.reason
    report = {
        "reason": None if reason is None else reason.value,
        "voice_check": check.as_dict(),
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if reason is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
