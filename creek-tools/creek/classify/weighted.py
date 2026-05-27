"""Shared weighted-classification model (issue #365).

PR #359 (issue #350) introduced :class:`WeightedDimension` and a
weighted-tuple-per-dimension schema for free-form essay prompts in
:mod:`creek.classify.prompt`. This module promotes those primitives —
the dataclass, the parser helpers, the allowed-top-level-keys set —
to a shared location so fragment-level callers (atoms in the
holarchy, sub-issues #366 / #367) can reuse them without inverting
the dependency graph (epic #364).

Public surface:

- :class:`WeightedDimension` — frozen generic dataclass holding a
  ``(value, weight)`` pair for a single ontology-dimension entry.
- :class:`WeightedFragmentClassification` — Pydantic model that lives
  on :class:`creek.models.Fragment.weighted`, mirroring
  :class:`creek.classify.prompt.PromptOntology` minus the ``prompt``
  field. Pydantic so it round-trips through vault YAML frontmatter via
  ``Fragment.model_dump(mode="json")`` / ``Fragment.model_validate``
  without bespoke serialiser plumbing.
- Parser helpers (:func:`_coerce_weight`, :func:`_parse_enum_value`,
  :func:`_parse_dimension`, :func:`_load_yaml_dict`) and the
  :data:`_ALLOWED_TOP_LEVEL_KEYS` set are leading-underscore module
  privates that :mod:`creek.classify.prompt` (and #366's
  fragment-level classifier) import directly.

The legacy single-pick fields on :class:`creek.models.Fragment`
(``frequency``, ``wavelength``, ``voice``) remain authoritative for
downstream consumers (compile, lint, voice-skill generation). When
``weighted`` is populated, the two stay synchronised via the
:meth:`WeightedFragmentClassification.from_single_pick` and
:meth:`WeightedFragmentClassification.to_legacy` adapters; callers
that populate ``weighted`` are responsible for keeping the legacy
fields in sync until the broader epic completes and consumers migrate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict

# Import :func:`_strip_code_fences` from its defining submodule rather
# than the :mod:`creek.classify.llm` package surface. The package's
# ``__init__.py`` re-exports happen *after* every submodule loads, but
# this module is itself imported during ``creek.models`` finalisation
# while ``creek.classify.llm`` is still partially loaded — the package
# re-export isn't yet visible. Going to ``parsing.py`` directly side-
# steps the cycle without leaking the layering.
from creek.classify.llm.parsing import _strip_code_fences
from creek.models import (
    Color,
    Dosage,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from creek.models import Fragment

__all__ = [
    "WeightedDimension",
    "WeightedFragmentClassification",
]


_DimT = TypeVar("_DimT", bound=StrEnum)


@dataclass(frozen=True)
class WeightedDimension(Generic[_DimT]):
    """A single ontology-dimension value paired with a detection weight.

    Used inside :class:`WeightedFragmentClassification` and
    :class:`creek.classify.prompt.PromptOntology` to model "this
    fragment activates Phase ``rising`` at strength 0.7 and Phase
    ``bottoming_out`` at strength 0.4." Weights live in
    ``[0.0, 1.0]``; the parser clamps any out-of-range value so a
    misbehaving LLM cannot inject negative or super-unit weights into
    downstream consumers.

    Attributes:
        value: The dimension's canonical enum member.
        weight: Detection strength in ``[0.0, 1.0]``.
    """

    value: _DimT
    weight: float


# YAML top-level keys accepted by :func:`_load_yaml_dict`. Shared
# between the prompt-ontology detector (PR #359) and the fragment-level
# classifier landing in #366 so the schema is enforced from one place.
_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "frequencies",
        "phases",
        "modes",
        "orientations",
        "dosages",
        "voice_registers",
        "overall_confidence",
    },
)


# Per-aspect weight applied to legacy ``secondary`` frequencies when
# :meth:`WeightedFragmentClassification.from_single_pick` widens a
# Fragment's legacy classification into weighted form. Chosen so the
# primary's 1.0 weight clearly dominates after sorting while leaving
# the secondaries above the 0.05 LLM template floor (PR #359) so
# downstream composition does not silently drop them.
_LEGACY_SECONDARY_WEIGHT: float = 0.5

# Per-aspect weight applied to non-secondary legacy fields (phase,
# mode, orientation, dosage, voice register) when widening: each is a
# single canonical pick at full conviction.
_LEGACY_SINGLE_WEIGHT: float = 1.0


def _coerce_weight(value: object) -> float:
    """Coerce a YAML-loaded weight value into ``[0.0, 1.0]``.

    A non-numeric value is treated as zero rather than raising — the
    parser is intentionally lenient on per-entry weights so one bad
    entry cannot drop the whole detection. The clamp keeps downstream
    composition arithmetic stable (negative weights would invert
    rankings; weights > 1 would amplify a single dimension over the
    others without bound).

    Args:
        value: Raw YAML scalar; expected to be a float-like number.

    Returns:
        A float in ``[0.0, 1.0]``.
    """
    if isinstance(value, bool):
        return 0.0
    if not isinstance(value, (int, float)):
        return 0.0
    weight = float(value)
    if not math.isfinite(weight):
        # NaN slips past both clamp branches because NaN comparisons
        # always return False, and ±inf would amplify rankings without
        # bound. Both collapse to zero so downstream composition stays
        # finite.
        return 0.0
    if weight < 0.0:
        return 0.0
    if weight > 1.0:
        return 1.0
    return weight


def _parse_enum_value(value: object, enum_type: type[_DimT]) -> _DimT | None:
    """Resolve a YAML scalar to a :class:`StrEnum` member, or ``None``.

    Used by :func:`_parse_dimension` to skip unknown values silently
    rather than raise — the LLM may emit a value that doesn't map to
    a canonical enum member (a typo, an outdated alias), and the
    detector's job is to return what it can rather than abort.

    Args:
        value: Raw YAML scalar.
        enum_type: The target :class:`StrEnum` subclass.

    Returns:
        The matching enum member, or ``None`` if no match exists.
    """
    if value is None:
        return None
    val_str = str(value).lower().strip()
    if not val_str:
        return None
    for member in enum_type:
        if member.value.lower() == val_str:
            return member
    return None


def _parse_dimension(
    data: dict[str, object],
    key: str,
    enum_type: type[_DimT],
) -> tuple[WeightedDimension[_DimT], ...]:
    """Parse a single dimension's list of ``{value, weight}`` entries.

    Tolerates the common failure modes the LLM produces in practice:

    * the dimension key is missing entirely (return empty tuple);
    * the dimension's value is not a list (return empty tuple);
    * an individual entry lacks a ``value`` field or has an unknown
      value (drop the entry);
    * an individual entry lacks a ``weight`` field (admit at zero so
      the caller still sees the model picked it).

    Entries are returned sorted by weight descending so the heaviest
    signal sits at index 0; ties preserve the LLM's YAML response order
    because :py:meth:`list.sort` is stable.

    Args:
        data: Parsed top-level YAML dict.
        key: Top-level key for this dimension (``"frequencies"`` etc.).
        enum_type: Target :class:`StrEnum` subclass for the dimension.

    Returns:
        Tuple of weighted entries, sorted by weight descending.
    """
    raw = data.get(key)
    if not isinstance(raw, list):
        return ()
    parsed: list[WeightedDimension[_DimT]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        enum_value = _parse_enum_value(entry.get("value"), enum_type)
        if enum_value is None:
            continue
        weight = _coerce_weight(entry.get("weight"))
        parsed.append(WeightedDimension(value=enum_value, weight=weight))
    parsed.sort(key=lambda wd: wd.weight, reverse=True)
    return tuple(parsed)


def _load_yaml_dict(yaml_text: str) -> dict[str, object]:
    """Parse the YAML payload and assert the documented shape.

    Mirrors :func:`creek.classify.llm.parsing.validate_response` but
    against the weighted-classification schema. Strips markdown fences
    first so a model that nests its YAML inside ```yaml ... ``` round-
    trips correctly. Rejects multi-document payloads (a classic
    LLM-output-spoofing vector) and any top-level keys outside
    :data:`_ALLOWED_TOP_LEVEL_KEYS`.

    Args:
        yaml_text: The YAML body extracted from the LLM response.

    Returns:
        The parsed dict.

    Raises:
        ValueError: On multi-document payloads, non-dict roots, or
            unexpected top-level keys.
    """
    text = _strip_code_fences(yaml_text)
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        msg = f"Invalid YAML in weighted-classification response: {exc}"
        raise ValueError(msg) from exc
    if len(docs) > 1:
        msg = f"multi-document YAML response rejected ({len(docs)} documents)"
        raise ValueError(msg)
    parsed: object = docs[0] if docs else None
    if not isinstance(parsed, dict):
        msg = f"Expected YAML dict, got {type(parsed).__name__}"
        # TRY004 would have us raise TypeError, but ValueError matches
        # the documented schema-validation contract on this helper and
        # on every caller; switching to TypeError would break the
        # documented Raises section without functional benefit.
        raise ValueError(msg)  # noqa: TRY004
    keys = {str(k) for k in parsed}
    extras = keys - _ALLOWED_TOP_LEVEL_KEYS
    if extras:
        msg = (
            "unexpected top-level keys in weighted-classification response: "
            f"{sorted(extras)}"
        )
        raise ValueError(msg)
    return {str(k): v for k, v in parsed.items()}


class WeightedFragmentClassification(BaseModel):
    """Weighted ontology profile attached to a :class:`Fragment`.

    Mirrors :class:`creek.classify.prompt.PromptOntology` minus the
    ``prompt`` field. Each dimension is a tuple of
    :class:`WeightedDimension` entries sorted weight-descending so the
    heaviest signal sits at index 0.

    Lives on :attr:`Fragment.weighted` as ``WeightedFragmentClassification |
    None``; ``None`` means "no weighted detection ran" (legacy vaults,
    pre-#366 fragments). When set, the legacy single-pick fields
    (:attr:`Fragment.frequency`, :attr:`Fragment.wavelength`,
    :attr:`Fragment.voice`) can be derived from the top weighted pick
    per dimension via :meth:`to_legacy`; the reverse widening lives in
    :meth:`from_single_pick`.

    Pydantic BaseModel rather than a frozen dataclass (the
    :class:`PromptOntology` shape) so the field plugs into
    :class:`Fragment` without bespoke serialiser plumbing — the
    Pydantic v2 default of round-tripping ``tuple[<dataclass>, ...]``
    through ``model_dump(mode="json")`` / ``model_validate`` is
    sufficient.
    """

    model_config = ConfigDict(frozen=True)

    frequencies: tuple[WeightedDimension[Frequency], ...] = ()
    phases: tuple[WeightedDimension[Phase], ...] = ()
    modes: tuple[WeightedDimension[Mode], ...] = ()
    orientations: tuple[WeightedDimension[Orientation], ...] = ()
    dosages: tuple[WeightedDimension[Dosage], ...] = ()
    voice_registers: tuple[WeightedDimension[VoiceRegister], ...] = ()
    overall_confidence: float = 0.0
    reasoning: str = ""

    @classmethod
    def from_single_pick(
        cls,
        fragment: Fragment,
    ) -> WeightedFragmentClassification:
        """Widen a legacy single-pick :class:`Fragment` into weighted form.

        Each non-``UNCLASSIFIED`` legacy field becomes a single
        :class:`WeightedDimension` entry; the primary frequency gets
        weight 1.0 and each secondary frequency gets
        :data:`_LEGACY_SECONDARY_WEIGHT`. ``UNCLASSIFIED`` fields are
        dropped so :meth:`to_legacy` can distinguish "unset" from
        "explicitly classified as unclassified".

        The following legacy state does **not** round-trip back via
        :meth:`to_legacy` (documented limitations):

        * :attr:`WavelengthClassification.color` is **not** carried
          across; :meth:`to_legacy` recomputes it from the top
          frequency via :data:`_FREQUENCY_TO_COLOR`.
        * :attr:`WavelengthClassification.descriptor` is a free-form
          string with no weighted analogue; :meth:`to_legacy` returns
          ``""``.
        * :attr:`VoiceClassification.confidence` is a
          :class:`Confidence` enum with no weighted analogue;
          :meth:`to_legacy` returns ``None``.

        Args:
            fragment: A Pydantic ``Fragment`` instance.

        Returns:
            A :class:`WeightedFragmentClassification` reflecting every
            non-``UNCLASSIFIED`` legacy field on the input.
        """
        frequencies = _widen_frequency(fragment.frequency)
        phases = _widen_single(fragment.wavelength.phase, Phase)
        modes = _widen_single(fragment.wavelength.mode, Mode)
        orientations = _widen_single(fragment.wavelength.orientation, Orientation)
        dosages = _widen_single(fragment.wavelength.dosage, Dosage)
        voice_registers = _widen_voice_register(fragment.voice.voice_register)

        return cls(
            frequencies=frequencies,
            phases=phases,
            modes=modes,
            orientations=orientations,
            dosages=dosages,
            voice_registers=voice_registers,
        )

    def to_legacy(
        self,
    ) -> tuple[FrequencyClassification, WavelengthClassification, VoiceClassification]:
        """Collapse weighted tuples back into legacy single-pick form.

        The top entry per dimension becomes the legacy single pick;
        remaining frequencies become :attr:`FrequencyClassification.secondary`.
        Empty tuples collapse to ``UNCLASSIFIED`` so a default-empty
        :class:`WeightedFragmentClassification` round-trips to the
        same default-empty legacy classifications.

        See :meth:`from_single_pick` for the documented limitations
        (color is recomputed; descriptor and voice confidence are
        dropped).

        Returns:
            A 3-tuple of ``(FrequencyClassification,
            WavelengthClassification, VoiceClassification)`` ready to
            assign back onto a :class:`Fragment`.
        """
        primary, secondary = _split_frequencies(self.frequencies)
        phase = _top_value(self.phases, Phase.UNCLASSIFIED)
        mode = _top_value(self.modes, Mode.UNCLASSIFIED)
        orientation = _top_value(self.orientations, Orientation.UNCLASSIFIED)
        dosage = _top_value(self.dosages, Dosage.UNCLASSIFIED)
        voice_register = _top_value_optional(self.voice_registers)
        color = _FREQUENCY_TO_COLOR.get(primary, Color.UNCLASSIFIED)

        return (
            FrequencyClassification(primary=primary, secondary=secondary),
            WavelengthClassification(
                phase=phase,
                mode=mode,
                orientation=orientation,
                dosage=dosage,
                color=color,
                descriptor="",
            ),
            VoiceClassification(
                voice_register=voice_register,
                confidence=None,
            ),
        )


# ---- Adapter helpers -------------------------------------------------------


# Canonical frequency → Spiral-Dynamics colour mapping. Mirrored from
# the prompt-template constants in :mod:`creek.classify.llm.prompts`
# but kept local here to avoid pulling the whole prompt-building
# module into adapter code paths. The two must stay in sync; the
# fragment-level classifier landing in #366 will exercise both, which
# anchors the contract.
_FREQUENCY_TO_COLOR: dict[Frequency, Color] = {
    Frequency.F1: Color.BEIGE,
    Frequency.F2: Color.PURPLE,
    Frequency.F3: Color.RED,
    Frequency.F4: Color.BLUE,
    Frequency.F5: Color.ORANGE,
    Frequency.F6: Color.GREEN,
    Frequency.F7: Color.YELLOW,
    Frequency.F8: Color.TEAL,
    Frequency.F9: Color.ULTRAVIOLET,
    Frequency.F10: Color.CLEAR_LIGHT,
}


def _widen_frequency(
    legacy: FrequencyClassification,
) -> tuple[WeightedDimension[Frequency], ...]:
    """Widen a legacy frequency classification into weighted entries.

    The primary gets weight 1.0 (or is dropped if ``UNCLASSIFIED``);
    each non-``UNCLASSIFIED`` secondary gets
    :data:`_LEGACY_SECONDARY_WEIGHT`. Duplicates between primary and
    secondaries are deduplicated, preserving the primary's higher
    weight — legacy vaults occasionally list the primary in
    ``secondary`` too.

    Args:
        legacy: The Fragment's ``frequency`` field.

    Returns:
        A weight-descending tuple of weighted frequencies.
    """
    entries: list[WeightedDimension[Frequency]] = []
    seen: set[Frequency] = set()
    primary = _coerce_enum(legacy.primary, Frequency)
    if primary is not None and primary is not Frequency.UNCLASSIFIED:
        entries.append(WeightedDimension(value=primary, weight=_LEGACY_SINGLE_WEIGHT))
        seen.add(primary)
    for raw in legacy.secondary:
        member = _coerce_enum(raw, Frequency)
        if member is None or member is Frequency.UNCLASSIFIED or member in seen:
            continue
        entries.append(
            WeightedDimension(value=member, weight=_LEGACY_SECONDARY_WEIGHT),
        )
        seen.add(member)
    return tuple(entries)


def _widen_single(
    legacy: _DimT | str,
    enum_type: type[_DimT],
) -> tuple[WeightedDimension[_DimT], ...]:
    """Widen a single legacy enum field into a one-entry weighted tuple.

    ``UNCLASSIFIED`` collapses to the empty tuple so :meth:`to_legacy`
    can distinguish "unset" from "explicitly classified as
    unclassified". The legacy field arrives as either an enum member
    (when constructed through Python) or its string value (when
    loaded from YAML with ``use_enum_values=True``); both forms are
    accepted.

    Args:
        legacy: The legacy enum value, possibly serialised as a string.
        enum_type: The target :class:`StrEnum` subclass.

    Returns:
        A single-entry tuple at weight 1.0, or an empty tuple when
        the input is ``UNCLASSIFIED`` / unresolvable.
    """
    member = _coerce_enum(legacy, enum_type)
    unclassified = enum_type("unclassified")
    if member is None or member is unclassified:
        return ()
    return (WeightedDimension(value=member, weight=_LEGACY_SINGLE_WEIGHT),)


def _widen_voice_register(
    legacy: VoiceRegister | str | None,
) -> tuple[WeightedDimension[VoiceRegister], ...]:
    """Widen :attr:`VoiceClassification.voice_register` into weighted form.

    Voice register has no ``UNCLASSIFIED`` sentinel; its absence is
    modelled as ``None``. ``None`` widens to the empty tuple so the
    round-trip distinguishes "no voice signal" from "explicitly the
    confessional register".

    Args:
        legacy: The voice register, possibly ``None``.

    Returns:
        A single-entry tuple at weight 1.0, or empty when ``None``.
    """
    if legacy is None:
        return ()
    member = _coerce_enum(legacy, VoiceRegister)
    if member is None:
        return ()
    return (WeightedDimension(value=member, weight=_LEGACY_SINGLE_WEIGHT),)


def _coerce_enum(value: object, enum_type: type[_DimT]) -> _DimT | None:
    """Coerce a Pydantic-loaded legacy enum value back into the enum.

    Pydantic's ``ConfigDict(use_enum_values=True)`` on :class:`Fragment`
    means legacy enum fields come back as raw strings, not enum
    members, after a YAML round-trip. The widening helpers need the
    enum members to compare against ``UNCLASSIFIED`` and to anchor
    :class:`WeightedDimension`'s ``value`` field, so this helper
    normalises both forms.

    Args:
        value: An enum member or its string value.
        enum_type: The target :class:`StrEnum` subclass.

    Returns:
        The matching enum member, or ``None`` for inputs that do not
        round-trip cleanly (an unrelated string, an unrelated enum).
    """
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            return None
    return None


def _split_frequencies(
    weighted: tuple[WeightedDimension[Frequency], ...],
) -> tuple[Frequency, list[Frequency]]:
    """Split a weighted-frequency tuple into legacy ``(primary, secondary)``.

    The top entry by weight becomes the primary; the rest become
    secondaries in weight-descending order. An empty tuple collapses
    to ``(UNCLASSIFIED, [])``.

    Args:
        weighted: A weight-descending tuple of weighted frequencies.

    Returns:
        ``(primary, secondary)`` ready for
        :class:`FrequencyClassification`.
    """
    if not weighted:
        return Frequency.UNCLASSIFIED, []
    primary = weighted[0].value
    secondary = [entry.value for entry in weighted[1:]]
    return primary, secondary


def _top_value(
    weighted: tuple[WeightedDimension[_DimT], ...],
    default: _DimT,
) -> _DimT:
    """Return the highest-weight entry's value, or ``default`` when empty."""
    if not weighted:
        return default
    return weighted[0].value


def _top_value_optional(
    weighted: tuple[WeightedDimension[_DimT], ...],
) -> _DimT | None:
    """Return the highest-weight entry's value, or ``None`` when empty."""
    if not weighted:
        return None
    return weighted[0].value


# Fragment.weighted's forward reference is resolved at the bottom of
# :mod:`creek.models` via a late import of this module; we do not call
# ``Fragment.model_rebuild`` here ourselves so the rebuild has a single
# source of truth and the import dependency stays models → weighted
# rather than mutually recursive at runtime.
