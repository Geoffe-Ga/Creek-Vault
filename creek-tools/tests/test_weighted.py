"""Tests for the shared weighted-classification model (issue #365).

Covers :mod:`creek.classify.weighted` — the foundation laid by epic
#364 that promotes PR #359's :class:`WeightedDimension` to a module
both the prompt detector and the (forthcoming) fragment classifier
share, and introduces :class:`WeightedFragmentClassification`, the
Pydantic model that lives on :attr:`Fragment.weighted`.

Three concerns get exercised here:

1. **Public surface.** ``WeightedDimension`` and parser helpers are
   re-exported from :mod:`creek.classify.prompt` so PR #359's import
   path keeps working — verified via direct attribute access on the
   prompt module.
2. **Fragment integration.** ``Fragment.weighted`` defaults to ``None``,
   round-trips byte-identically through Pydantic's JSON-mode
   serialiser, and a legacy YAML payload without a ``weighted`` key
   loads with ``weighted=None``.
3. **Single-pick ↔ weighted adapters.** ``from_single_pick`` widens a
   legacy ``Fragment`` and ``to_legacy`` collapses back; the pair is
   inverse on non-``UNCLASSIFIED`` legacy fields modulo the documented
   limitations (color is recomputed; descriptor and voice confidence
   are dropped).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from creek.classify import prompt as prompt_module
from creek.classify import weighted as weighted_module
from creek.classify.weighted import (
    _FREQUENCY_TO_COLOR,
    _LEGACY_SECONDARY_WEIGHT,
    _LEGACY_SINGLE_WEIGHT,
    WeightedDimension,
    WeightedFragmentClassification,
    _coerce_weight,
    _load_yaml_dict,
    _parse_dimension,
    _parse_enum_value,
)
from creek.models import (
    Color,
    Confidence,
    Dosage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

# ---- Fixtures --------------------------------------------------------------


def _make_fragment(**overrides: object) -> Fragment:
    """Build a minimal :class:`Fragment` with optional field overrides.

    The default fragment is the same shape ``test_minimal_creation``
    uses in :mod:`tests.test_models` so behaviour stays comparable
    across the test suite.

    Args:
        **overrides: Field values to splice onto the default Fragment.

    Returns:
        A constructed :class:`Fragment` ready for use in a test body.
    """
    defaults: dict[str, object] = {
        "id": "frag-000000000001",
        "title": "Test Fragment",
        "source": FragmentSource(platform=SourcePlatform.CLAUDE),
    }
    defaults.update(overrides)
    return Fragment(**defaults)  # type: ignore[arg-type]


# ---- Public-surface tests --------------------------------------------------


class TestPublicSurface:
    """The promoted symbols are exposed from both modules."""

    def test_weighted_dimension_re_exported_from_prompt(self) -> None:
        """PR #359's :class:`WeightedDimension` import path still works."""
        # The class object on both modules is the same.
        assert prompt_module.WeightedDimension is WeightedDimension

    def test_parser_helpers_re_exported_from_prompt(self) -> None:
        """Helper functions are accessible on the prompt module too."""
        assert (
            prompt_module._coerce_weight is _coerce_weight
        )  # private re-export check matches the documented contract
        assert prompt_module._parse_dimension is _parse_dimension
        assert prompt_module._load_yaml_dict is _load_yaml_dict

    def test_module_all_exports(self) -> None:
        """``weighted.py``'s ``__all__`` names the public-API symbols."""
        assert set(weighted_module.__all__) == {
            "WeightedDimension",
            "WeightedFragmentClassification",
        }


# ---- Parser helper tests ---------------------------------------------------


class TestCoerceWeight:
    """The leading-underscore parser helpers move cleanly to weighted.py."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.5, 0.5),
            (0.0, 0.0),
            (1.0, 1.0),
            (-0.1, 0.0),
            (1.5, 1.0),
            (None, 0.0),
            ("0.5", 0.0),
            (True, 0.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (float("-inf"), 0.0),
        ],
    )
    def test_clamps_and_rejects(self, value: object, expected: float) -> None:
        """The clamp keeps weights inside ``[0.0, 1.0]`` for downstream math."""
        assert _coerce_weight(value) == expected


class TestParseEnumValue:
    """Enum lookups are case-insensitive and tolerate unknown values."""

    def test_resolves_known_value(self) -> None:
        """A canonical string maps to its enum member."""
        assert _parse_enum_value("F3", Frequency) is Frequency.F3

    def test_returns_none_for_unknown(self) -> None:
        """An unknown value falls through to ``None``."""
        assert _parse_enum_value("not-a-frequency", Frequency) is None

    def test_returns_none_for_none(self) -> None:
        """Explicit ``None`` short-circuits to ``None``."""
        assert _parse_enum_value(None, Frequency) is None

    def test_case_insensitive(self) -> None:
        """Case differences resolve to the canonical member."""
        assert _parse_enum_value("RISING", Phase) is Phase.RISING

    def test_empty_string_after_strip_returns_none(self) -> None:
        """Whitespace-only input collapses to ``None`` rather than misresolving."""
        assert _parse_enum_value("   ", Frequency) is None


class TestLoadYamlDict:
    """The dictionary validator rejects multi-doc payloads and stray keys."""

    def test_extra_top_level_key_rejected(self) -> None:
        """A stray top-level key produces a schema ``ValueError``."""
        with pytest.raises(ValueError, match="unexpected top-level keys"):
            _load_yaml_dict("frequencies: []\nbogus: 1")

    def test_known_keys_pass(self) -> None:
        """All canonical top-level keys are accepted."""
        text = (
            "frequencies: []\n"
            "phases: []\n"
            "modes: []\n"
            "orientations: []\n"
            "dosages: []\n"
            "voice_registers: []\n"
            "overall_confidence: 0.5\n"
        )
        parsed = _load_yaml_dict(text)
        assert parsed["overall_confidence"] == 0.5

    def test_non_dict_entry_dropped(self) -> None:
        """``_parse_dimension`` skips non-dict entries silently."""
        # A YAML list containing a scalar mixed with a valid entry —
        # the scalar is dropped, the valid entry survives. Anchors the
        # defensive ``isinstance(entry, dict)`` guard.
        data: dict[str, object] = {
            "frequencies": ["bogus", {"value": "F3", "weight": 0.7}],
        }
        parsed = _parse_dimension(data, "frequencies", Frequency)
        assert parsed == (WeightedDimension(value=Frequency.F3, weight=0.7),)

    def test_invalid_voice_register_widens_to_empty(self) -> None:
        """A non-enum string on ``voice_register`` collapses to an empty tuple."""
        # Bypass Pydantic's enum coercion by going through ``model_validate``
        # with a typo — Pydantic would raise; we hand-roll the legacy
        # widening helper directly to exercise the defensive branch.
        from creek.classify.weighted import _widen_voice_register

        assert _widen_voice_register("not-a-register") == ()  # type: ignore[arg-type]


class TestCoerceEnumEdgeCases:
    """``_coerce_enum`` tolerates enum members, strings, and outright junk."""

    def test_unrelated_string_returns_none(self) -> None:
        """A string that doesn't match any member of the enum returns ``None``."""
        from creek.classify.weighted import _coerce_enum

        assert _coerce_enum("not-a-frequency", Frequency) is None

    def test_unrelated_object_returns_none(self) -> None:
        """A non-string, non-enum input returns ``None`` rather than raising."""
        from creek.classify.weighted import _coerce_enum

        assert _coerce_enum(42, Frequency) is None


# ---- WeightedFragmentClassification construction ---------------------------


class TestWeightedFragmentClassificationConstruction:
    """Direct construction matches PromptOntology's documented shape."""

    def test_default_is_empty(self) -> None:
        """Default-constructed instance has empty tuples and zero confidence."""
        empty = WeightedFragmentClassification()
        assert empty.frequencies == ()
        assert empty.phases == ()
        assert empty.modes == ()
        assert empty.orientations == ()
        assert empty.dosages == ()
        assert empty.voice_registers == ()
        assert empty.overall_confidence == 0.0
        assert empty.reasoning == ""

    def test_construct_with_dimensions(self) -> None:
        """Explicit tuples flow through unchanged."""
        weighted = WeightedFragmentClassification(
            frequencies=(WeightedDimension(value=Frequency.F3, weight=0.8),),
            phases=(WeightedDimension(value=Phase.RISING, weight=0.7),),
            overall_confidence=0.75,
            reasoning="brief reasoning",
        )
        assert weighted.frequencies[0].value is Frequency.F3
        assert weighted.frequencies[0].weight == 0.8
        assert weighted.phases[0].value is Phase.RISING
        assert weighted.overall_confidence == 0.75
        assert weighted.reasoning == "brief reasoning"

    def test_frozen_blocks_mutation(self) -> None:
        """The model is frozen so callers cannot mutate it in place."""
        weighted = WeightedFragmentClassification(overall_confidence=0.5)
        # Pydantic v2 raises ``ValidationError`` on a frozen-instance
        # write; the test cares about the immutability contract, not
        # the exact exception subclass.
        with pytest.raises(ValidationError):
            weighted.overall_confidence = 0.9


# ---- Fragment.weighted integration -----------------------------------------


class TestFragmentWeightedField:
    """The ``weighted`` field defaults to ``None`` and round-trips clean."""

    def test_default_is_none(self) -> None:
        """A minimal Fragment carries ``weighted=None``."""
        frag = _make_fragment()
        assert frag.weighted is None

    def test_explicit_weighted_is_carried(self) -> None:
        """A constructed ``Fragment`` keeps the provided weighted value."""
        wfc = WeightedFragmentClassification(
            frequencies=(WeightedDimension(value=Frequency.F3, weight=0.8),),
            overall_confidence=0.7,
        )
        frag = _make_fragment(weighted=wfc)
        assert frag.weighted is wfc

    def test_roundtrip_byte_identical_for_populated(self) -> None:
        """``model_dump(mode='json')`` + ``model_validate`` is identity."""
        wfc = WeightedFragmentClassification(
            frequencies=(
                WeightedDimension(value=Frequency.F3, weight=0.8),
                WeightedDimension(value=Frequency.F5, weight=0.4),
            ),
            phases=(WeightedDimension(value=Phase.RISING, weight=0.6),),
            modes=(WeightedDimension(value=Mode.EXPRESS, weight=0.5),),
            orientations=(WeightedDimension(value=Orientation.DO_FEEL, weight=0.7),),
            dosages=(
                WeightedDimension(value=Dosage.TOXIC, weight=0.6),
                WeightedDimension(value=Dosage.MEDICINE, weight=0.3),
            ),
            voice_registers=(
                WeightedDimension(value=VoiceRegister.ANALYTICAL, weight=0.5),
            ),
            overall_confidence=0.7,
            reasoning="A representative reasoning preamble",
        )
        frag = _make_fragment(weighted=wfc)
        dumped = frag.model_dump(mode="json")
        loaded = Fragment.model_validate(dumped)
        assert loaded.weighted == frag.weighted

    def test_roundtrip_byte_identical_for_none(self) -> None:
        """A ``weighted=None`` Fragment also round-trips cleanly."""
        frag = _make_fragment()
        loaded = Fragment.model_validate(frag.model_dump(mode="json"))
        assert loaded.weighted is None

    def test_legacy_payload_without_weighted_key_loads(self) -> None:
        """A YAML-style dict lacking ``weighted`` loads with ``weighted=None``.

        Mirrors the on-disk reality of pre-#365 vault fragments where
        the frontmatter does not contain a ``weighted`` block at all.
        """
        payload = {
            "id": "frag-000000000099",
            "title": "Legacy fragment",
            "source": {"platform": "claude"},
        }
        loaded = Fragment.model_validate(payload)
        assert loaded.weighted is None


# ---- from_single_pick / to_legacy adapters ---------------------------------


class TestFromSinglePick:
    """Widening legacy single-pick fields into weighted form."""

    def test_unclassified_widens_to_empty(self) -> None:
        """A default Fragment (all-unclassified) widens to all-empty tuples."""
        frag = _make_fragment()
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.frequencies == ()
        assert widened.phases == ()
        assert widened.modes == ()
        assert widened.orientations == ()
        assert widened.dosages == ()
        assert widened.voice_registers == ()

    def test_primary_frequency_widens_to_single_full_weight(self) -> None:
        """A primary-only frequency becomes one entry at weight 1.0."""
        frag = _make_fragment(
            frequency=FrequencyClassification(primary=Frequency.F3),
        )
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.frequencies == (
            WeightedDimension(value=Frequency.F3, weight=_LEGACY_SINGLE_WEIGHT),
        )

    def test_secondaries_widen_at_secondary_weight(self) -> None:
        """Secondary frequencies widen at the secondary weight, primary at 1.0."""
        frag = _make_fragment(
            frequency=FrequencyClassification(
                primary=Frequency.F3,
                secondary=[Frequency.F5, Frequency.F7],
            ),
        )
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.frequencies == (
            WeightedDimension(value=Frequency.F3, weight=_LEGACY_SINGLE_WEIGHT),
            WeightedDimension(value=Frequency.F5, weight=_LEGACY_SECONDARY_WEIGHT),
            WeightedDimension(value=Frequency.F7, weight=_LEGACY_SECONDARY_WEIGHT),
        )

    def test_secondaries_with_duplicate_of_primary_dedupe(self) -> None:
        """A legacy ``secondary`` repeating the primary is deduplicated."""
        frag = _make_fragment(
            frequency=FrequencyClassification(
                primary=Frequency.F3,
                secondary=[Frequency.F3, Frequency.F5],
            ),
        )
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.frequencies == (
            WeightedDimension(value=Frequency.F3, weight=_LEGACY_SINGLE_WEIGHT),
            WeightedDimension(value=Frequency.F5, weight=_LEGACY_SECONDARY_WEIGHT),
        )

    def test_wavelength_widens_each_dimension(self) -> None:
        """Every non-unclassified wavelength axis becomes a one-entry tuple."""
        frag = _make_fragment(
            wavelength=WavelengthClassification(
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
                orientation=Orientation.DO_FEEL,
                dosage=Dosage.MEDICINE,
            ),
        )
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.phases == (
            WeightedDimension(value=Phase.RISING, weight=_LEGACY_SINGLE_WEIGHT),
        )
        assert widened.modes == (
            WeightedDimension(value=Mode.EXPRESS, weight=_LEGACY_SINGLE_WEIGHT),
        )
        assert widened.orientations == (
            WeightedDimension(value=Orientation.DO_FEEL, weight=_LEGACY_SINGLE_WEIGHT),
        )
        assert widened.dosages == (
            WeightedDimension(value=Dosage.MEDICINE, weight=_LEGACY_SINGLE_WEIGHT),
        )

    def test_voice_register_set_widens(self) -> None:
        """An explicit voice_register widens; ``None`` collapses to empty."""
        frag = _make_fragment(
            voice=VoiceClassification(voice_register=VoiceRegister.ANALYTICAL),
        )
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.voice_registers == (
            WeightedDimension(
                value=VoiceRegister.ANALYTICAL,
                weight=_LEGACY_SINGLE_WEIGHT,
            ),
        )

    def test_voice_register_none_widens_to_empty(self) -> None:
        """A Fragment without a voice register widens to an empty tuple."""
        frag = _make_fragment()
        widened = WeightedFragmentClassification.from_single_pick(frag)
        assert widened.voice_registers == ()


class TestToLegacy:
    """Collapsing weighted tuples back into legacy classifications."""

    def test_empty_collapses_to_unclassified(self) -> None:
        """A default-empty weighted instance collapses to all-unclassified."""
        empty = WeightedFragmentClassification()
        frequency, wavelength, voice = empty.to_legacy()
        assert frequency.primary == Frequency.UNCLASSIFIED
        assert frequency.secondary == []
        assert wavelength.phase == Phase.UNCLASSIFIED
        assert wavelength.mode == Mode.UNCLASSIFIED
        assert wavelength.orientation == Orientation.UNCLASSIFIED
        assert wavelength.dosage == Dosage.UNCLASSIFIED
        assert wavelength.color == Color.UNCLASSIFIED
        assert wavelength.descriptor == ""
        assert voice.voice_register is None
        assert voice.confidence is None

    def test_top_frequency_becomes_primary(self) -> None:
        """The highest-weight frequency lands on ``primary``; rest on ``secondary``."""
        wfc = WeightedFragmentClassification(
            frequencies=(
                WeightedDimension(value=Frequency.F3, weight=0.9),
                WeightedDimension(value=Frequency.F5, weight=0.4),
                WeightedDimension(value=Frequency.F7, weight=0.2),
            ),
        )
        frequency, _wavelength, _voice = wfc.to_legacy()
        assert frequency.primary == Frequency.F3
        assert frequency.secondary == [Frequency.F5, Frequency.F7]

    def test_color_derived_from_top_frequency(self) -> None:
        """Color comes from the canonical frequency→color mapping."""
        wfc = WeightedFragmentClassification(
            frequencies=(WeightedDimension(value=Frequency.F3, weight=0.9),),
        )
        _frequency, wavelength, _voice = wfc.to_legacy()
        assert wavelength.color == Color.RED  # F3 → red per the canonical map

    @pytest.mark.parametrize(
        "freq",
        [
            Frequency.F1,
            Frequency.F2,
            Frequency.F3,
            Frequency.F4,
            Frequency.F5,
            Frequency.F6,
            Frequency.F7,
            Frequency.F8,
            Frequency.F9,
            Frequency.F10,
        ],
    )
    def test_color_mapping_complete(self, freq: Frequency) -> None:
        """Every concrete frequency has a colour assignment."""
        assert _FREQUENCY_TO_COLOR[freq] is not Color.UNCLASSIFIED


class TestRoundTrip:
    """The widening / collapsing pair is inverse on non-unclassified fields."""

    def test_widen_then_collapse_preserves_legacy_fields(self) -> None:
        """``from_single_pick`` + ``to_legacy`` round-trip on the supported fields."""
        original = _make_fragment(
            frequency=FrequencyClassification(
                primary=Frequency.F3,
                secondary=[Frequency.F5, Frequency.F7],
            ),
            wavelength=WavelengthClassification(
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
                orientation=Orientation.DO_FEEL,
                dosage=Dosage.MEDICINE,
                color=Color.RED,
            ),
            voice=VoiceClassification(voice_register=VoiceRegister.ANALYTICAL),
        )
        widened = WeightedFragmentClassification.from_single_pick(original)
        freq_out, wave_out, voice_out = widened.to_legacy()
        assert freq_out.primary == Frequency.F3
        assert freq_out.secondary == [Frequency.F5, Frequency.F7]
        assert wave_out.phase == Phase.RISING
        assert wave_out.mode == Mode.EXPRESS
        assert wave_out.orientation == Orientation.DO_FEEL
        assert wave_out.dosage == Dosage.MEDICINE
        assert wave_out.color == Color.RED  # recomputed from primary, matches original
        assert voice_out.voice_register == VoiceRegister.ANALYTICAL

    def test_widen_drops_voice_confidence(self) -> None:
        """``VoiceClassification.confidence`` does not round-trip (documented)."""
        original = _make_fragment(
            voice=VoiceClassification(
                voice_register=VoiceRegister.PROPHETIC,
                confidence=Confidence.SETTLED,
            ),
        )
        widened = WeightedFragmentClassification.from_single_pick(original)
        _freq, _wave, voice_out = widened.to_legacy()
        # voice_register survives; confidence is dropped to None.
        assert voice_out.voice_register == VoiceRegister.PROPHETIC
        assert voice_out.confidence is None

    def test_widen_drops_descriptor(self) -> None:
        """``WavelengthClassification.descriptor`` does not round-trip."""
        original = _make_fragment(
            wavelength=WavelengthClassification(
                phase=Phase.RISING,
                descriptor="A free-form descriptor that has no weighted analogue",
            ),
        )
        widened = WeightedFragmentClassification.from_single_pick(original)
        _freq, wave_out, _voice = widened.to_legacy()
        assert wave_out.phase == Phase.RISING
        assert wave_out.descriptor == ""

    def test_widen_drops_pre_existing_color(self) -> None:
        """Color is recomputed from the top frequency, not preserved verbatim."""
        # Constructing a fragment with a deliberately mismatched colour
        # (F3 paired with TEAL — F3's canonical colour is RED) anchors
        # the contract: ``to_legacy`` ignores the legacy colour field
        # and recomputes from ``primary``.
        original = _make_fragment(
            frequency=FrequencyClassification(primary=Frequency.F3),
            wavelength=WavelengthClassification(
                phase=Phase.RISING,
                color=Color.TEAL,
            ),
        )
        widened = WeightedFragmentClassification.from_single_pick(original)
        _freq, wave_out, _voice = widened.to_legacy()
        assert wave_out.color == Color.RED  # recomputed from F3, not TEAL


# ---- Generic enum-coercion edge cases --------------------------------------


class TestEnumCoercionEdgeCases:
    """The widening helpers tolerate both enum and string-valued inputs."""

    def test_widen_frequency_handles_string_primary(self) -> None:
        """A Fragment loaded from YAML keeps enum semantics through widening.

        Pydantic's ``use_enum_values=True`` on Fragment means the
        on-disk YAML stores enum strings, not enum members. After a
        ``model_validate`` round-trip, ``frequency.primary`` comes back
        as a plain string. The widening helpers must tolerate that.
        """
        # Round-trip through YAML so primary lands as a string.
        original = _make_fragment(
            frequency=FrequencyClassification(primary=Frequency.F3),
        )
        loaded = Fragment.model_validate(original.model_dump(mode="json"))
        # ``primary`` is now a string per ``use_enum_values=True``.
        assert loaded.frequency.primary == "F3"

        widened = WeightedFragmentClassification.from_single_pick(loaded)
        assert widened.frequencies == (
            WeightedDimension(value=Frequency.F3, weight=_LEGACY_SINGLE_WEIGHT),
        )
