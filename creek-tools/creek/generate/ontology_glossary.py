"""Registry of bespoke Creek-ontology terms and their plain-English gloss seeds.

Drafts written in the owner's voice lean on bespoke vocabulary — APTITUDE
**Frequencies** (``F1`` through ``F10`` and their labels), Archetypal-Wavelength
**Phases** (Rising, Peaking, …), engagement **Modes**, developmental
**altitudes** (Teal, Ultraviolet), and named concepts (APTITUDE, the Whole
Adept, the Archetypal Wavelength, the non-dual substrate). A newcomer cannot
follow prose that uses these as if the reader were already an initiate.

This module is the **single source of truth** for that vocabulary. Every
:class:`OntologyTerm` is *derived* from the existing taxonomy constants — the
``Frequency`` / ``Phase`` / ``Mode`` enums and the ``FREQUENCY_*`` / ``PHASE_*``
description maps — rather than re-typed by hand, so the glossary cannot silently
drift from the taxonomy. Each derived term names the taxonomy member it came
from in its ``source`` field, and the drift-guard test matches every
non-sentinel member to the term declaring it **by identity**. Identity is
load-bearing: all three taxonomies are ``StrEnum`` classes, so their members
compare *and hash* as bare strings across classes, and all three
``UNCLASSIFIED`` sentinels are the one string ``"unclassified"``. The older
guard asserted only that ``member.value`` resolved to *some* registry key, which
one enum's term could satisfy on another enum's behalf — that is how the
sentinels slipped through (#1343).

The three ``UNCLASSIFIED`` sentinels earn no term at all: "we could not classify
this" is a wire value, not vocabulary a draft ever writes. Each builder excludes
its own sentinel with ``is``, never ``==``, which would also match the other two
taxonomies' sentinels.

The seeds are short hints (a clause's worth), not dictionary definitions: they
exist to ground the model so it can phrase the gloss *in the owner's voice* on
first mention, and to give the conservative ``unglossed_jargon`` detector a
keyword signal that an explanation is nearby.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING

from creek.generate.indexes import (
    CANONICAL_FREQUENCY_NAMES,
    FREQUENCY_COLORS,
    FREQUENCY_THEMES,
)
from creek.generate.wavelength import _PHASE_DESCRIPTIONS
from creek.models import Frequency, Mode, Phase

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Plain-English activation sense for each engagement mode, adapted from the
#: ``_mode_activation_phrase`` base map in :mod:`creek.generate.skills`. Kept
#: here as a short gloss seed (not the full activation phrasing). Deliberately
#: keyed only by the modes the glossary glosses — the ``UNCLASSIFIED`` sentinel
#: is not vocabulary and is excluded from the glossary — so a future ``Mode``
#: member added without a gloss seed raises ``KeyError`` when the terms are
#: built. That build-time failure is the drift detection working, not a bug.
#:
#: Keying a dict by an enum member is the shape #1343 warns about, and it is
#: safe *here only* because this map is mono-taxonomy and excludes the sentinel:
#: no two ``Mode`` members share a value, and ``UNCLASSIFIED`` — the one value
#: that also belongs to ``Frequency`` and ``Phase`` — is not a key. Do not
#: generalise the pattern to a map spanning more than one taxonomy.
_MODE_SENSES: dict[Mode, str] = {
    Mode.INHABIT: "living inside the frequency rather than performing it",
    Mode.EXPRESS: "enacting the frequency outward, for others to feel",
    Mode.COLLABORATE: "working a frequency alongside other people",
    Mode.INTEGRATE: "metabolising a frequency into a coherent whole",
    Mode.ABSORB: "receiving a frequency rather than producing it",
}

#: Developmental-altitude colour terms surfaced in prose (e.g. "Ultraviolet").
#: Sourced from :data:`FREQUENCY_COLORS` (the ontology colour designations) so
#: the altitude vocabulary tracks the frequency taxonomy. Each maps to the
#: frequency whose theme grounds its gloss seed.
_ALTITUDE_COLORS: dict[str, Frequency] = {
    "Teal": Frequency.F8,
    "Ultraviolet": Frequency.F9,
}

#: Named bespoke concepts that are not enum members but read as jargon to a
#: newcomer. Each carries its own short gloss seed grounded in the ontology.
_NAMED_CONCEPTS: dict[str, str] = {
    "APTITUDE": (
        "my ten-frequency map of human motivation, from raw survival up to "
        "egoless emptiness"
    ),
    "Whole Adept": (
        "someone fluent across the whole range of frequencies, able to move "
        "between them at will"
    ),
    "Archetypal Wavelength": (
        "the six-phase rise-and-fall cycle any current of energy moves through"
    ),
    "non-dual substrate": (
        "the ground where the line between self and world stops feeling real"
    ),
}


@dataclass(frozen=True)
class OntologyTerm:
    """One bespoke ontology term with the seed for its plain-English gloss.

    Attributes:
        label: The surface form a draft writes (e.g. ``"F8"``, ``"Peaking"``,
            ``"Ultraviolet"``, ``"APTITUDE"``).
        category: Which family the term belongs to — ``"frequency"``,
            ``"phase"``, ``"mode"``, ``"altitude"`` or ``"concept"``.
        gloss_seed: A short, owner-voiced hint the model can expand into an
            appositive gloss on first mention; also the keyword signal the
            detector looks for nearby.
        aliases: Extra surface forms that name the same term (e.g. a frequency's
            APTITUDE label alongside its ``F``-code).
        source: The taxonomy member this term is derived from, if any. Carried
            so the drift guard can match a member to its term by *identity*
            rather than by string resolution — the three taxonomies are
            ``StrEnum`` classes whose members are interchangeable as strings.
            ``None`` for terms with no enum behind them (altitudes and named
            concepts). Note that it joins the frozen dataclass's generated
            ``__eq__`` / ``__hash__``, where the same cross-taxonomy string
            equality applies; two terms are kept distinct by their labels, which
            :func:`_build_registry` already guarantees are unique, so never
            lean on ``source`` alone to tell two terms apart.
    """

    label: str
    category: str
    gloss_seed: str
    aliases: tuple[str, ...] = ()
    source: Frequency | Phase | Mode | None = None

    def __post_init__(self) -> None:
        """Reject any surface form that is a bare serialised wire value.

        A single all-lower-case word (``"rising"``, ``"bottoming_out"``) is an
        enum's ``.value``, indistinguishable from ordinary English — the
        detector matches surface forms case-sensitively on a word boundary, so
        registering one makes it flag prose (#1343). A lower-case *phrase*
        (``"non-dual substrate"``) is real vocabulary and stays legal, as does
        anything carrying a capital (``"F1"``, ``"Rising"``, ``"APTITUDE"``).

        Raises:
            ValueError: If ``label`` or any entry of ``aliases`` is a single
                all-lower-case word.
        """
        for surface in (self.label, *self.aliases):
            if surface.islower() and " " not in surface:
                msg = (
                    f"ontology surface form {surface!r} is a bare lower-case "
                    "word — a serialised wire value, indistinguishable from "
                    "ordinary English; register the prose form a draft writes"
                )
                raise ValueError(msg)


def _frequency_terms() -> list[OntologyTerm]:
    """Build a term per real ``Frequency`` member from the theme/name maps.

    The canonical APTITUDE name is kept as an alias: unlike a wire value, it is
    a capitalised proper name ("Agency", "Pluralism") a draft genuinely writes.
    """
    return [
        OntologyTerm(
            label=freq.value,
            category="frequency",
            gloss_seed=FREQUENCY_THEMES[freq],
            aliases=(CANONICAL_FREQUENCY_NAMES[freq],),
            source=freq,
        )
        for freq in Frequency
        if freq is not Frequency.UNCLASSIFIED
    ]


def _phase_terms() -> list[OntologyTerm]:
    """Build a term per real ``Phase`` member from the phase-description map.

    The title-cased label ("Rising", "Bottoming Out") is the only surface form a
    draft writes; the ``.value`` behind it is a wire value, never registered.
    """
    return [
        OntologyTerm(
            label=phase.name.replace("_", " ").title(),
            category="phase",
            gloss_seed=_PHASE_DESCRIPTIONS[phase.value],
            source=phase,
        )
        for phase in Phase
        if phase is not Phase.UNCLASSIFIED
    ]


def _mode_terms() -> list[OntologyTerm]:
    """Build a term per real ``Mode`` member from the activation-sense map.

    As with phases, only the title-cased label is a surface form — "absorb" and
    "express" as bare wire values are ordinary English verbs.
    """
    return [
        OntologyTerm(
            label=mode.value.title(),
            category="mode",
            gloss_seed=_MODE_SENSES[mode],
            source=mode,
        )
        for mode in Mode
        if mode is not Mode.UNCLASSIFIED
    ]


def _altitude_terms() -> list[OntologyTerm]:
    """Build a term per developmental-altitude colour from the colour map."""
    terms: list[OntologyTerm] = []
    for label, freq in _ALTITUDE_COLORS.items():
        # Cross-check the colour map so the altitude vocabulary cannot drift
        # from the frequency taxonomy it is derived from.
        if FREQUENCY_COLORS[freq] != label.lower():  # pragma: no cover
            continue
        terms.append(
            OntologyTerm(
                label=label,
                category="altitude",
                gloss_seed=(
                    f"the altitude of {FREQUENCY_THEMES[freq].split(',')[0].lower()}"
                ),
            )
        )
    return terms


def _concept_terms() -> list[OntologyTerm]:
    """Build a term per named bespoke concept."""
    return [
        OntologyTerm(label=label, category="concept", gloss_seed=seed)
        for label, seed in _NAMED_CONCEPTS.items()
    ]


def iter_ontology_terms() -> list[OntologyTerm]:
    """Return every bespoke ontology term, derived from the taxonomy constants.

    Returns:
        The frequencies, phases, modes, altitudes and named concepts, each with
        its plain-English gloss seed.
    """
    return [
        *_frequency_terms(),
        *_phase_terms(),
        *_mode_terms(),
        *_altitude_terms(),
        *_concept_terms(),
    ]


def _source_key(source: Frequency | Phase | Mode) -> tuple[str, str]:
    """Return a key that distinguishes *source* across the three taxonomies.

    ``Frequency`` / ``Phase`` / ``Mode`` are ``StrEnum`` classes, so a member is
    equal to — and hashes as — its bare value: keying by the member itself would
    collapse the three ``UNCLASSIFIED`` sentinels into one. Pairing the class
    name with the value keeps each member distinct.
    """
    return (type(source).__name__, source.value)


def _check_source_collisions(terms: Sequence[OntologyTerm]) -> None:
    """Raise ValueError if two *terms* derive from the same taxonomy member.

    Terms with no ``source`` (altitudes, named concepts) are skipped: they
    derive from no member, so they cannot collide on one.

    Raises:
        ValueError: If two terms declare the same taxonomy member as ``source``,
            which would leave the drift guard matching that member to whichever
            of them it happened to see first.
    """
    seen: dict[tuple[str, str], OntologyTerm] = {}
    for term in terms:
        if term.source is None:
            continue
        key = _source_key(term.source)
        previous = seen.get(key)
        if previous is not None:
            msg = (
                f"two ontology terms derive from {key[0]}.{key[1]}: "
                f"{previous.label!r} and {term.label!r}"
            )
            raise ValueError(msg)
        seen[key] = term


def _build_registry(terms: Sequence[OntologyTerm]) -> dict[str, OntologyTerm]:
    """Index *terms* by their exact surface forms, refusing any collision.

    Kept pure and un-memoised so it can be exercised directly;
    :func:`ontology_term_registry` is the cached, read-only view of it over
    :func:`iter_ontology_terms`.

    Args:
        terms: The ontology terms to index.

    Returns:
        A mapping from each exact surface form (a term's ``label`` and every
        entry of its ``aliases``) to the term that owns it.

    Raises:
        ValueError: If two terms claim the same surface form — the second would
            silently shadow the first, leaving a term that can never be glossed
            or detected. Source collisions raise here too, via
            :func:`_check_source_collisions`.
    """
    _check_source_collisions(terms)
    registry: dict[str, OntologyTerm] = {}
    for term in terms:
        for surface in (term.label, *term.aliases):
            previous = registry.get(surface)
            if previous is not None:
                msg = (
                    f"two ontology terms claim the surface form {surface!r}: "
                    f"{previous.label!r} ({previous.category}) and "
                    f"{term.label!r} ({term.category})"
                )
                raise ValueError(msg)
            registry[surface] = term
    return registry


@cache
def ontology_term_registry() -> Mapping[str, OntologyTerm]:
    """Return a lookup from each exact surface form to the term that owns it.

    A term's ``label`` and each of its ``aliases`` is registered under its
    **exact** form and nothing else. This once wrote a lower-cased shadow key
    beside each one, which is what made the detector flag ordinary English: the
    detector matches case-sensitively, so the shadow turned every phase and mode
    wire value (``rising``, ``absorb``) into a matchable term (#1343). Which
    surface forms may exist at all is enforced at the source, in
    :meth:`OntologyTerm.__post_init__`.

    Two terms claiming one surface form is an error, not a last-writer-wins
    overwrite; :func:`_build_registry` raises rather than lose a term.

    The registry is derived from static enums and constants, so it is built once
    and memoised. The result is wrapped in a read-only
    :class:`~types.MappingProxyType` so a caller cannot mutate the shared cached
    instance.

    Returns:
        A read-only mapping from exact surface form to :class:`OntologyTerm`.
    """
    return MappingProxyType(_build_registry(iter_ontology_terms()))


#: Prompt steer asking the model to gloss each bespoke term in the owner's voice
#: on its *first* mention. Appended to every ``## Ask`` variant on both the draft
#: and voice surfaces and pinned by structure tests so it can never silently drop
#: out of the prompt. Phrased as a request for an appositive/short clause — not a
#: textbook definition — and explicitly first-mention-only ("only once").
GLOSS_STEER: str = (
    "The first time you use one of my bespoke terms (a Frequency, Phase, Mode, "
    "altitude, or named concept like APTITUDE / Whole Adept / Archetypal "
    "Wavelength), weave in a brief plain-English gloss in my voice so a newcomer "
    "can follow — an appositive or short clause, not a dictionary definition, "
    "and don't lecture. Gloss each term only once."
)
