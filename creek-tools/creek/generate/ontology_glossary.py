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
drift from the taxonomy. A drift-guard test asserts every enum member resolves
to an entry, so a new enum member without a gloss seed fails the build.

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
    from collections.abc import Mapping

#: Plain-English activation sense for each engagement mode, adapted from the
#: ``_mode_activation_phrase`` base map in :mod:`creek.generate.skills`. Kept
#: here as a short gloss seed (not the full activation phrasing).
_MODE_SENSES: dict[Mode, str] = {
    Mode.INHABIT: "living inside the frequency rather than performing it",
    Mode.EXPRESS: "enacting the frequency outward, for others to feel",
    Mode.COLLABORATE: "working a frequency alongside other people",
    Mode.INTEGRATE: "metabolising a frequency into a coherent whole",
    Mode.ABSORB: "receiving a frequency rather than producing it",
    Mode.UNCLASSIFIED: "no dominant engagement mode is in play",
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
    """

    label: str
    category: str
    gloss_seed: str
    aliases: tuple[str, ...] = ()


def _frequency_terms() -> list[OntologyTerm]:
    """Build a term per ``Frequency`` member from the theme/name maps."""
    terms: list[OntologyTerm] = []
    for freq in Frequency:
        if freq is Frequency.UNCLASSIFIED:
            seed = "no dominant frequency signal is present"
            aliases: tuple[str, ...] = ()
        else:
            seed = FREQUENCY_THEMES[freq]
            aliases = (CANONICAL_FREQUENCY_NAMES[freq],)
        terms.append(
            OntologyTerm(
                label=freq.value,
                category="frequency",
                gloss_seed=seed,
                aliases=aliases,
            )
        )
    return terms


def _phase_terms() -> list[OntologyTerm]:
    """Build a term per ``Phase`` member from the phase-description map."""
    return [
        OntologyTerm(
            label=phase.name.replace("_", " ").title(),
            category="phase",
            gloss_seed=_PHASE_DESCRIPTIONS[phase.value],
            aliases=(phase.value,),
        )
        for phase in Phase
    ]


def _mode_terms() -> list[OntologyTerm]:
    """Build a term per ``Mode`` member from the activation-sense map."""
    return [
        OntologyTerm(
            label=mode.value.title(),
            category="mode",
            gloss_seed=_MODE_SENSES[mode],
            aliases=(mode.value,),
        )
        for mode in Mode
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


@cache
def ontology_term_registry() -> Mapping[str, OntologyTerm]:
    """Return a lookup of every term surface form (lower-cased) to its term.

    Both each term's ``label`` and its ``aliases`` are registered under their
    exact surface form *and* a lower-cased form, so a drift-guard test can assert
    ``freq.value in registry`` for every enum member while case-insensitive
    lookups (an altitude or named concept written mid-sentence) still resolve.

    The registry is derived from static enums and constants, so it is built once
    and memoised. The result is wrapped in a read-only
    :class:`~types.MappingProxyType` so a caller cannot mutate the shared cached
    instance.

    Returns:
        A read-only mapping from surface form (exact and lower-cased) to
        :class:`OntologyTerm`.
    """
    registry: dict[str, OntologyTerm] = {}
    for term in iter_ontology_terms():
        for surface in (term.label, *term.aliases):
            registry[surface] = term
            registry[surface.lower()] = term
    return MappingProxyType(registry)


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
