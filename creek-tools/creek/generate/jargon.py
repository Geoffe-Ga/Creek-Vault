"""Conservative detector for un-glossed bespoke ontology jargon.

Scans a drafted body for the bespoke terms in
:func:`~creek.generate.ontology_glossary.ontology_term_registry` and, for each
term's **first** occurrence only, flags it when no explanatory gloss appears in
a small window after it. A second occurrence is never flagged — the owner's
contract is to gloss each term once, on first mention.

The detector is deliberately biased toward **false negatives**: it would rather
miss an un-glossed term than wrongly flag a term that already carries a nearby
explanation. A first mention is considered glossed when the window after it
holds any gloss signal — an em-dash, a parenthetical, an appositive comma, a
``which is`` / ``the … where`` clause, or one of the term's own gloss-seed
keywords. Surface forms are matched case-sensitively on a word boundary so an
ordinary lower-case word (``rising`` the verb) or a capitalised non-term
(``London``) never trips the scan.

Case-sensitivity only buys that where the registry holds no bare lower-case
form to match. Every registered surface form is prose a draft writes — a label
("Rising", "Ultraviolet") or a proper name ("Agency") — and a serialised wire
value (``rising``, ``absorb``) can never be registered at all: constructing an
:class:`~creek.generate.ontology_glossary.OntologyTerm` around a single
all-lower-case word raises, in its ``__post_init__``. That guard is what stands
between this module and the #1343 defect: re-adding a ``.value`` alias would,
absent it, put the wire values back in the scan's path and make this module flag
ordinary English again. Weakening the guard and re-adding the alias together is
the one change that silently restores the bug.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass

from creek.generate.ontology_glossary import OntologyTerm, ontology_term_registry

#: Characters/clauses that, appearing in the window after a first mention, count
#: as an explanatory gloss signal (an appositive, parenthetical or relative
#: clause). Kept small and high-precision so the detector errs toward misses.
_GLOSS_MARKERS: tuple[str, ...] = (
    "—",  # em-dash appositive: "Ultraviolet -- the altitude where ..."
    "\u2013",  # en-dash, used the same way
    " - ",  # spaced hyphen appositive
    "(",  # parenthetical: "Peaking (the full expression …)"
    "which is",
    "which means",
    ", the ",  # comma appositive: "F8, the voice of my higher self"
    ": the ",  # colon appositive: "Peaking: the full expression …"
    ", my ",  # comma appositive in first person: "F8, my true self speaking"
)

#: How many characters after a first mention to scan for a gloss signal. A gloss
#: is an appositive or short clause, so it sits right next to the term; a wide
#: window would let an unrelated later sentence falsely "explain" the term.
_GLOSS_WINDOW: int = 90

#: Minimum length for a gloss-seed word to count as a nearby-explanation signal.
#: Short words (``the``, ``of``) carry no topical weight.
_MIN_SEED_WORD: int = 4


@dataclass(frozen=True)
class UnglossedJargonFinding:
    """One bespoke term used on first mention without a nearby gloss.

    Attributes:
        term: The surface form that was used un-glossed (e.g. ``"Ultraviolet"``).
        severity: Always ``"MID"`` — a soft finding, since a false hit costs at
            most one revise round, never a hard block.
        message: A human-readable explanation naming the term and its gloss seed.
    """

    term: str
    severity: str
    message: str


def _seed_keywords(term: OntologyTerm) -> set[str]:
    """Return the significant (>= 4-char) lower-cased words of a term's seed."""
    pattern = rf"[a-z]{{{_MIN_SEED_WORD},}}"
    return set(re.findall(pattern, term.gloss_seed.lower()))


def _window_has_gloss(window: str, term: OntologyTerm) -> bool:
    """Return whether *window* (text just after a first mention) glosses *term*.

    A gloss is present when the window carries any structural marker (em-dash,
    parenthetical, appositive comma, ``which is`` clause) or mentions one of the
    term's own gloss-seed keywords.
    """
    if any(marker in window for marker in _GLOSS_MARKERS):
        return True
    lowered = window.lower()
    return any(keyword in lowered for keyword in _seed_keywords(term))


def _first_match(body: str, surface: str) -> re.Match[str] | None:
    """Return the first word-boundary, case-sensitive match of *surface*."""
    return re.search(rf"\b{re.escape(surface)}\b", body)


def _earliest_mention(body: str, term: OntologyTerm) -> re.Match[str] | None:
    """Return the earliest match of any of *term*'s surface forms in *body*."""
    best: re.Match[str] | None = None
    for surface in (term.label, *term.aliases):
        match = _first_match(body, surface)
        if match is not None and (best is None or match.start() < best.start()):
            best = match
    return best


def _build_finding(match: re.Match[str], term: OntologyTerm) -> UnglossedJargonFinding:
    """Build the ``MID`` finding for an un-glossed first mention *match*."""
    return UnglossedJargonFinding(
        term=match.group(),
        severity="MID",
        message=(
            f"Bespoke term {match.group()!r} is used on first mention "
            "without a plain-English gloss a newcomer could follow; "
            f"weave in a short clause (seed: {term.gloss_seed!r})."
        ),
    )


def detect_unglossed_jargon(body: str) -> list[UnglossedJargonFinding]:
    """Flag each bespoke term whose first mention carries no nearby gloss.

    For every distinct registered term, the earliest surface form that appears
    in *body* is located; if no gloss signal sits in the window after it, one
    ``MID`` finding is raised. Only the first mention is considered, so a later
    repeat of the same term is never separately flagged.

    Args:
        body: The drafted prose under review.

    Returns:
        One :class:`UnglossedJargonFinding` per un-glossed first mention,
        ordered by where the term first appears in the body.
    """
    if not body.strip():
        return []
    # Collapse aliases: judge each term once, at its earliest surface form.
    findings: list[tuple[int, UnglossedJargonFinding]] = []
    for term in dict.fromkeys(ontology_term_registry().values()):
        match = _earliest_mention(body, term)
        if match is None:
            continue
        window = body[match.end() : match.end() + _GLOSS_WINDOW]
        if _window_has_gloss(window, term):
            continue
        findings.append((match.start(), _build_finding(match, term)))
    return [finding for _start, finding in sorted(findings, key=operator.itemgetter(0))]
