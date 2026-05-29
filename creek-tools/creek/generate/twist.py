"""Ontology-twist composition for ``creek draft``.

The default composition path retrieves source material per-dimension
and asks the LLM to weave it. That is enough when the operator wants a
faithful synthesis of their own voice, but the goal here is **"your
ideas with a twist"** — drafts whose conceptual
content comes from the user's vault but whose voice signatures (phase,
mode, frequency, dosage, register) honour a *target* combination the
operator has asked for.

This module computes the ingredients the prompt template needs:

* :class:`OntologyProfile` — a frozen snapshot of a dimensional signature
  with one canonical value per dimension. Empty (``"unspecified"``) when
  no signal exists for that dimension.
* :func:`source_profile_from_fragments` — derive the source profile by
  taking the dominant non-unclassified value across the retrieved
  fragments. This is the "what the user actually wrote about" signal.
* :func:`target_profile_from_spec` — derive the target profile from the
  operator's explicit flags + detected :class:`PromptOntology`. This is
  "what the operator wants the draft to sound like".
* :func:`twist_dimensions` — the ordered list of dimensions where source
  and target diverge. Empty list = no twist (the prompt collapses to the
  legacy per-dimension composition; regression baseline).
* :func:`format_twist_directive` — render the prompt block that names
  the divergent dimensions and forbids verbatim paraphrase.
* :class:`PluralitySourceError` — raised by
  :func:`require_plural_sources` when twist composition is requested with
  only one source fragment. Recombination requires more than one input;
  failing loudly is the documented behaviour.

The module is import-side-effect-free and has no LLM dependency, so the
twist machinery is fully unit-testable independent of provider
configuration.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.models import Dosage, Frequency, Mode, Phase

if TYPE_CHECKING:
    from creek.classify.prompt import PromptOntology
    from creek.generate.drafts import SeedSpec
    from creek.models import Fragment


UNSPECIFIED: str = "unspecified"
"""Sentinel value used when no signal exists for a dimension.

The string form is friendly to YAML frontmatter and string equality
checks against the StrEnum-backed dimension values stored on
:class:`Fragment`. ``"unspecified"`` collides with no real enum value.
"""


_DIMENSION_ORDER: tuple[str, ...] = (
    "phase",
    "mode",
    "frequency",
    "dosage",
    "voice_register",
)
"""Stable order for serialising and comparing dimensions.

Used by :func:`twist_dimensions` to keep the diff order deterministic.
"""


_MIN_PLURAL_SOURCES: int = 2
"""Minimum number of source fragments required for twist composition.

Recombination across fewer than two sources collapses to single-source
paraphrase, the failure mode the epic is escaping.
"""


@dataclass(frozen=True)
class OntologyProfile:
    """A canonical one-pick-per-dimension snapshot of an ontology signature.

    Attributes:
        phase: Wavelength phase value or :data:`UNSPECIFIED`.
        mode: Engagement mode value or :data:`UNSPECIFIED`.
        frequency: APTITUDE frequency code or :data:`UNSPECIFIED`.
        dosage: Dosage value or :data:`UNSPECIFIED`.
        voice_register: Voice register value or :data:`UNSPECIFIED`.
    """

    phase: str = UNSPECIFIED
    mode: str = UNSPECIFIED
    frequency: str = UNSPECIFIED
    dosage: str = UNSPECIFIED
    voice_register: str = UNSPECIFIED

    def as_mapping(self) -> dict[str, str]:
        """Return a plain dict for YAML frontmatter serialisation.

        Returns:
            ``{dimension_name: value}`` in :data:`_DIMENSION_ORDER`.
        """
        return {
            "phase": self.phase,
            "mode": self.mode,
            "frequency": self.frequency,
            "dosage": self.dosage,
            "voice_register": self.voice_register,
        }

    def get(self, dimension: str) -> str:
        """Return the value for *dimension* by name.

        Args:
            dimension: One of :data:`_DIMENSION_ORDER`.

        Returns:
            The dimension's value, or :data:`UNSPECIFIED` if the name is
            unknown. Unknown-name graceful return keeps the diff helper
            in :func:`twist_dimensions` simple.
        """
        return self.as_mapping().get(dimension, UNSPECIFIED)


class PluralitySourceError(ValueError):
    """Raised when twist composition has fewer than two source fragments.

    Recombination requires more than one input. The CLI surfaces the
    message verbatim so the operator can decide whether to widen the
    filters or ingest more sources.
    """


def _dominant(values: list[str]) -> str:
    """Return the most common non-empty / non-unclassified value, else UNSPECIFIED.

    Ties resolve by lexical order on the value for deterministic output
    across Python implementations. ``"unclassified"`` and empty strings
    are filtered out before counting so they cannot win the vote when
    the corpus carries a real signal alongside them.
    """
    counts: dict[str, int] = {}
    for value in values:
        if not value or value == "unclassified":
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return UNSPECIFIED
    return max(counts.items(), key=operator.itemgetter(1, 0))[0]


def source_profile_from_fragments(
    fragments: list[Fragment],
) -> OntologyProfile:
    """Aggregate *fragments* into a single canonical :class:`OntologyProfile`.

    The dominant non-unclassified value per dimension wins. When a
    dimension has no classified value across the whole list, the field
    is left as :data:`UNSPECIFIED` so :func:`twist_dimensions` can
    treat it as "no signal" rather than a divergent pick.

    Args:
        fragments: The retrieved source fragments. Typically the union
            of the per-dimension corpus slices.

    Returns:
        A frozen :class:`OntologyProfile` reflecting the corpus's
        dominant signatures.
    """
    if not fragments:
        return OntologyProfile()
    return OntologyProfile(
        phase=_dominant([str(f.wavelength.phase) for f in fragments]),
        mode=_dominant([str(f.wavelength.mode) for f in fragments]),
        frequency=_dominant([str(f.frequency.primary) for f in fragments]),
        dosage=_dominant([str(f.wavelength.dosage) for f in fragments]),
        voice_register=_dominant(
            [
                str(f.voice.voice_register)
                for f in fragments
                if f.voice.voice_register is not None
            ],
        ),
    )


def _heaviest_value(entries: tuple[object, ...], unclassified: object) -> str:
    """Return the heaviest above-zero, non-unclassified value as a string.

    The ``PromptOntology`` parser sorts entries by weight descending, so
    the heaviest above-:data:`UNSPECIFIED` value is the first qualifying
    entry. Returns :data:`UNSPECIFIED` when every entry is below or
    equal to zero or every entry is the unclassified sentinel.
    """
    for entry in entries:
        weight = getattr(entry, "weight", 0.0)
        value = getattr(entry, "value", None)
        if value is None or value == unclassified:
            continue
        if weight <= 0.0:
            continue
        return str(value)
    return UNSPECIFIED


def target_profile_from_spec(
    spec: SeedSpec,
    *,
    ontology: PromptOntology | None = None,
) -> OntologyProfile:
    """Derive the target :class:`OntologyProfile` from *spec* and *ontology*.

    Explicit flags win over the detected ontology: when both name the
    same dimension, the explicit pick is used. Otherwise the heaviest
    above-zero ontology entry per dimension is used. Dimensions with
    neither an explicit flag nor an above-zero ontology entry stay as
    :data:`UNSPECIFIED`.

    The dosage and voice-register dimensions have no explicit-flag
    counterpart today; they come entirely from the ontology. Adding
    them to the profile lets the twist directive name those signals
    when they diverge — the #352 issue body specifically mentions
    different dosage and different register as twist axes.

    Args:
        spec: The operator's :class:`SeedSpec`.
        ontology: Optional :class:`PromptOntology` augmenting the
            explicit flags. Defaults to ``spec.ontology`` so callers can
            omit the keyword when the spec carries its own ontology.

    Returns:
        The target :class:`OntologyProfile`.
    """
    resolved_ontology = ontology if ontology is not None else spec.ontology
    phase = (
        spec.phases[0].value
        if spec.phases
        else _heaviest_value(
            resolved_ontology.phases if resolved_ontology else (),
            Phase.UNCLASSIFIED,
        )
    )
    mode = (
        spec.modes[0].value
        if spec.modes
        else _heaviest_value(
            resolved_ontology.modes if resolved_ontology else (),
            Mode.UNCLASSIFIED,
        )
    )
    frequency = (
        spec.frequencies[0].value
        if spec.frequencies
        else _heaviest_value(
            resolved_ontology.frequencies if resolved_ontology else (),
            Frequency.UNCLASSIFIED,
        )
    )
    dosage = _heaviest_value(
        resolved_ontology.dosages if resolved_ontology else (),
        Dosage.UNCLASSIFIED,
    )
    voice_register = _heaviest_value(
        resolved_ontology.voice_registers if resolved_ontology else (),
        # VoiceRegister has no UNCLASSIFIED member; pass a sentinel that
        # cannot match any real value so every above-zero entry is kept.
        object(),
    )
    return OntologyProfile(
        phase=phase,
        mode=mode,
        frequency=frequency,
        dosage=dosage,
        voice_register=voice_register,
    )


def twist_dimensions(
    source: OntologyProfile,
    target: OntologyProfile,
) -> list[str]:
    """Return the ordered list of dimensions where source ≠ target.

    A dimension is considered divergent only when **both** sides carry
    a concrete value and the values differ. When either side is
    :data:`UNSPECIFIED` the dimension is treated as "no signal" rather
    than a divergence — naming a divergence the operator never asked
    for would make the twist directive lie about the target.

    Args:
        source: Profile derived from the retrieved fragments.
        target: Profile derived from spec + ontology.

    Returns:
        Dimension names from :data:`_DIMENSION_ORDER`, divergent
        entries only, in stable order.
    """
    divergent: list[str] = []
    for dim in _DIMENSION_ORDER:
        src = source.get(dim)
        tgt = target.get(dim)
        if UNSPECIFIED in (src, tgt):
            continue
        if src != tgt:
            divergent.append(dim)
    return divergent


_DIMENSION_LABEL_BY_NAME: dict[str, str] = {
    "phase": "phase",
    "mode": "stance",
    "frequency": "frequency",
    "dosage": "dosage",
    "voice_register": "voice register",
}
"""Human-readable label per dimension for the twist directive.

Mirrors the wording in :func:`format_dimension_label` from
:mod:`creek.generate.dimensional_retrieval` so the prompt's twist
sentence reads naturally next to the per-dimension section headers.
"""


def _format_one_dimension_diff(
    dim: str,
    source: OntologyProfile,
    target: OntologyProfile,
) -> str:
    """Render one divergence line: ``phase: peaking → withdrawal``."""
    label = _DIMENSION_LABEL_BY_NAME.get(dim, dim)
    return f"- {label}: {source.get(dim)} -> {target.get(dim)}"


def format_twist_directive(
    source: OntologyProfile,
    target: OntologyProfile,
    divergent: list[str],
) -> str:
    """Render the ``## Twist directive`` prompt block.

    When *divergent* is empty the directive collapses to a single line
    that records the matching profile and asks the LLM to honour the
    activated skills as usual — preserving the regression baseline the
    #352 acceptance criterion calls for.

    Args:
        source: The source profile.
        target: The target profile.
        divergent: Dimensions where source and target diverge, as
            returned by :func:`twist_dimensions`.

    Returns:
        A markdown-flavoured prompt block, ready to be concatenated
        into :meth:`DraftGenerator._compose_prompt`.
    """
    header = "## Twist directive"
    if not divergent:
        return (
            f"{header}\n"
            "Source and target ontology profiles match on every detected "
            "dimension; compose in the operator's requested voice without "
            "additional re-framing."
        )
    diffs = "\n".join(
        _format_one_dimension_diff(dim, source, target) for dim in divergent
    )
    body = (
        "The source musings sit on a different ontology profile than the "
        "target combination the operator asked for. The dimensions that "
        "diverge are:\n"
        f"{diffs}\n\n"
        "Use the provided source musings as the ideas being explored, but "
        "voice them through the target combination's style. Do not "
        "paraphrase any single source verbatim. Recombine across the "
        "corpus."
    )
    return f"{header}\n{body}"


def require_plural_sources(source_fragments: tuple[str, ...]) -> None:
    """Raise :class:`PluralitySourceError` when fewer than two sources matched.

    The twist composition prompt explicitly asks the LLM to *recombine
    across the corpus*. Composing across a single source collapses to
    the legacy single-source paraphrase failure mode the epic is trying
    to escape — failing here is more honest than producing a draft that
    pretends to be a recombination.

    Args:
        source_fragments: The fragment IDs that will enter the prompt.

    Raises:
        PluralitySourceError: When ``len(source_fragments) < 2``.
    """
    if len(source_fragments) >= _MIN_PLURAL_SOURCES:
        return
    msg = (
        "Ontology-twist composition requires at least two source "
        f"fragments to recombine across; got {len(source_fragments)}. "
        "Widen the filters or ingest more sources before re-running."
    )
    raise PluralitySourceError(msg)


__all__ = [
    "UNSPECIFIED",
    "OntologyProfile",
    "PluralitySourceError",
    "format_twist_directive",
    "require_plural_sources",
    "source_profile_from_fragments",
    "target_profile_from_spec",
    "twist_dimensions",
]
