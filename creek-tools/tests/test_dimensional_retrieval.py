"""Tests for per-dimension corpus retrieval (issue #351).

Covers :mod:`creek.generate.dimensional_retrieval`: the OR-semantics
replacement for the legacy AND-intersection source-material assembly.
The pipeline-level tests live in ``test_drafts.py`` and verify the
DraftGenerator wiring; this file pins the helper module itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creek.classify.prompt import PromptOntology, WeightedDimension
from creek.generate.dimensional_retrieval import (
    AllDimensionsEmptyError,
    DimensionSlice,
    assemble_per_dimension_corpus,
    empty_dimensions,
    format_dimension_label,
    raise_if_all_empty,
    union_fragment_ids,
)
from creek.generate.drafts import SeedSpec
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    PraxisPotential,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)


def _fragment(
    *,
    frag_id: str = "frag",
    title: str = "title",
    primary: Frequency = Frequency.UNCLASSIFIED,
    phase: Phase = Phase.UNCLASSIFIED,
    mode: Mode = Mode.UNCLASSIFIED,
    orientation: Orientation = Orientation.UNCLASSIFIED,
    register: VoiceRegister | None = None,
) -> Fragment:
    """Return a minimal Fragment with the requested ontology dimensions."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.CLAUDE,
            original_file="scratch.md",
        ),
        created=datetime(2026, 3, 1, tzinfo=UTC),
        ingested=datetime(2026, 3, 1, tzinfo=UTC),
        frequency=FrequencyClassification(primary=primary),
        wavelength=WavelengthClassification(
            mode=mode,
            orientation=orientation,
            phase=phase,
        ),
        voice=VoiceClassification(
            voice_register=register,
            confidence=Confidence.SETTLED if register else None,
        ),
        praxis_potential=PraxisPotential.LATENT,
    )


def _loaded(*pairs: tuple[Fragment, str]) -> dict[str, tuple[Fragment, str]]:
    """Return a ``{id: (frag, body)}`` map from positional fragment/body pairs."""
    return {frag.id: (frag, body) for frag, body in pairs}


# ---- assemble_per_dimension_corpus ------------------------------------


class TestAssemblePerDimensionCorpus:
    """``assemble_per_dimension_corpus`` returns one slice per active dimension."""

    def test_explicit_flags_produce_unit_weighted_slices(self) -> None:
        """Each explicit CLI-flag dimension lands at weight 1.0."""
        frag_a = _fragment(frag_id="A", phase=Phase.PEAKING, mode=Mode.EXPRESS)
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec(phases=(Phase.PEAKING,), modes=(Mode.EXPRESS,))

        slices = assemble_per_dimension_corpus(spec, loaded)

        assert set(slices.keys()) == {("phase", "peaking"), ("mode", "express")}
        assert slices[("phase", "peaking")].weight == 1.0
        assert slices[("mode", "express")].weight == 1.0

    def test_or_semantics_unions_across_dimensions(self) -> None:
        """Fragments matching ANY dimension survive into their slice (OR, not AND)."""
        # frag-A matches phase only; frag-B matches mode only; frag-C matches both.
        # Under legacy AND, only frag-C would survive; under OR all three appear.
        frag_a = _fragment(frag_id="A", phase=Phase.PEAKING, mode=Mode.INHABIT)
        frag_b = _fragment(frag_id="B", phase=Phase.RISING, mode=Mode.EXPRESS)
        frag_c = _fragment(frag_id="C", phase=Phase.PEAKING, mode=Mode.EXPRESS)
        loaded = _loaded((frag_a, ""), (frag_b, ""), (frag_c, ""))
        spec = SeedSpec(phases=(Phase.PEAKING,), modes=(Mode.EXPRESS,))

        slices = assemble_per_dimension_corpus(spec, loaded)

        assert slices[("phase", "peaking")].fragment_ids == ("A", "C")
        assert slices[("mode", "express")].fragment_ids == ("B", "C")

    def test_empty_dimension_kept_with_empty_fragment_ids(self) -> None:
        """A dimension that matches nothing stays in the mapping with no fragments."""
        frag_a = _fragment(frag_id="A", phase=Phase.PEAKING)
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec(phases=(Phase.PEAKING,), modes=(Mode.EXPRESS,))

        slices = assemble_per_dimension_corpus(spec, loaded)

        assert slices[("mode", "express")].fragment_ids == ()
        assert slices[("mode", "express")].has_matches is False

    def test_topic_substring_filter_applies_per_slice(self) -> None:
        """Topic restricts each slice to fragments whose title/body contains it."""
        frag_a = _fragment(frag_id="A", phase=Phase.PEAKING, title="On belonging")
        frag_b = _fragment(frag_id="B", phase=Phase.PEAKING, title="Other")
        loaded = _loaded((frag_a, ""), (frag_b, "irrelevant body"))
        spec = SeedSpec(topic="belonging", phases=(Phase.PEAKING,))

        slices = assemble_per_dimension_corpus(spec, loaded)

        assert slices[("phase", "peaking")].fragment_ids == ("A",)

    def test_no_dimensions_returns_empty_mapping(self) -> None:
        """Topic-only specs produce no per-dimension slices (legacy path applies)."""
        frag_a = _fragment(frag_id="A")
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec(topic="belonging")

        slices = assemble_per_dimension_corpus(spec, loaded)

        assert slices == {}

    def test_ontology_contributes_weighted_dimensions(self) -> None:
        """An ontology's weighted phases/modes/frequencies become slices."""
        frag_a = _fragment(frag_id="A", phase=Phase.RISING)
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec()
        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.RISING, weight=0.7),),
        )

        slices = assemble_per_dimension_corpus(spec, loaded, ontology=ontology)

        assert slices[("phase", "rising")].weight == pytest.approx(0.7)
        assert slices[("phase", "rising")].fragment_ids == ("A",)

    def test_confidence_threshold_drops_low_weight_entries(self) -> None:
        """Ontology entries below the threshold are silently dropped."""
        frag_a = _fragment(frag_id="A", phase=Phase.RISING)
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec()
        ontology = PromptOntology(
            prompt="x",
            phases=(
                WeightedDimension(value=Phase.RISING, weight=0.7),
                WeightedDimension(value=Phase.PEAKING, weight=0.05),
            ),
        )

        slices = assemble_per_dimension_corpus(
            spec,
            loaded,
            ontology=ontology,
            confidence_threshold=0.5,
        )

        assert ("phase", "rising") in slices
        assert ("phase", "peaking") not in slices

    def test_explicit_flag_wins_when_ontology_repeats_key(self) -> None:
        """When ontology repeats an explicit-flag key, explicit weight (1.0) stays."""
        frag_a = _fragment(frag_id="A", phase=Phase.RISING)
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec(phases=(Phase.RISING,))
        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.RISING, weight=0.4),),
        )

        slices = assemble_per_dimension_corpus(spec, loaded, ontology=ontology)

        assert slices[("phase", "rising")].weight == 1.0

    def test_unclassified_ontology_value_dropped(self) -> None:
        """An ``UNCLASSIFIED`` ontology entry would match every fragment — drop it."""
        frag_a = _fragment(frag_id="A", phase=Phase.UNCLASSIFIED)
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec()
        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.UNCLASSIFIED, weight=0.9),),
        )

        slices = assemble_per_dimension_corpus(spec, loaded, ontology=ontology)

        assert slices == {}

    def test_explicit_keys_ordered_phase_mode_frequency(self) -> None:
        """Explicit-flag iteration order is deterministic across all three kinds."""
        frag_a = _fragment(
            frag_id="A",
            phase=Phase.PEAKING,
            mode=Mode.EXPRESS,
            primary=Frequency.F3,
        )
        loaded = _loaded((frag_a, ""))
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.EXPRESS,),
            frequencies=(Frequency.F3,),
        )

        keys = list(assemble_per_dimension_corpus(spec, loaded))

        assert keys == [
            ("phase", "peaking"),
            ("mode", "express"),
            ("frequency", "F3"),
        ]

    def test_frequency_dimension_matches_primary(self) -> None:
        """The frequency slice keys against ``fragment.frequency.primary``."""
        frag_a = _fragment(frag_id="A", primary=Frequency.F3)
        frag_b = _fragment(frag_id="B", primary=Frequency.F5)
        loaded = _loaded((frag_a, ""), (frag_b, ""))
        spec = SeedSpec(frequencies=(Frequency.F3,))

        slices = assemble_per_dimension_corpus(spec, loaded)

        assert slices[("frequency", "F3")].fragment_ids == ("A",)


# ---- helper accessors -------------------------------------------------


class TestDimensionHelpers:
    """Small accessors on the slice mapping."""

    def test_empty_dimensions_lists_only_empty_keys(self) -> None:
        """``empty_dimensions`` filters out slices that produced matches."""
        slices = {
            ("phase", "peaking"): DimensionSlice(
                key=("phase", "peaking"),
                weight=1.0,
                fragment_ids=("A",),
            ),
            ("mode", "express"): DimensionSlice(
                key=("mode", "express"),
                weight=1.0,
                fragment_ids=(),
            ),
        }
        assert empty_dimensions(slices) == [("mode", "express")]

    def test_union_fragment_ids_dedupes_first_seen_order(self) -> None:
        """The union preserves first-seen order and drops duplicates."""
        slices = {
            ("phase", "peaking"): DimensionSlice(
                key=("phase", "peaking"),
                weight=1.0,
                fragment_ids=("A", "C"),
            ),
            ("mode", "express"): DimensionSlice(
                key=("mode", "express"),
                weight=1.0,
                fragment_ids=("B", "C"),
            ),
        }
        assert union_fragment_ids(slices) == ("A", "C", "B")

    def test_format_dimension_label_known_kinds(self) -> None:
        """Each kind maps to the documented label phrasing."""
        assert format_dimension_label(("phase", "peaking")) == "the peaking phase"
        assert format_dimension_label(("mode", "express")) == "the express stance"
        assert format_dimension_label(("frequency", "F3")) == "the F3 frequency"

    def test_format_dimension_label_unknown_kind_falls_back(self) -> None:
        """Unknown kinds fall back to ``"kind value"`` rather than crashing."""
        assert format_dimension_label(("custom", "x")) == "custom x"


# ---- raise_if_all_empty -----------------------------------------------


class TestRaiseIfAllEmpty:
    """``raise_if_all_empty`` is the all-dimensions-empty guard."""

    def test_does_not_raise_on_empty_mapping(self) -> None:
        """An empty mapping means no dimensions were active — not an error."""
        raise_if_all_empty({})

    def test_does_not_raise_when_at_least_one_slice_has_matches(self) -> None:
        """If any slice has matches, generation should proceed."""
        slices = {
            ("phase", "peaking"): DimensionSlice(
                key=("phase", "peaking"),
                weight=1.0,
                fragment_ids=("A",),
            ),
            ("mode", "express"): DimensionSlice(
                key=("mode", "express"),
                weight=1.0,
                fragment_ids=(),
            ),
        }
        raise_if_all_empty(slices)

    def test_raises_when_every_slice_is_empty(self) -> None:
        """All-empty raises with each attempted dimension named in the message."""
        slices = {
            ("phase", "peaking"): DimensionSlice(
                key=("phase", "peaking"),
                weight=1.0,
                fragment_ids=(),
            ),
            ("mode", "express"): DimensionSlice(
                key=("mode", "express"),
                weight=1.0,
                fragment_ids=(),
            ),
        }
        with pytest.raises(AllDimensionsEmptyError) as exc:
            raise_if_all_empty(slices)
        assert "the peaking phase" in str(exc.value)
        assert "the express stance" in str(exc.value)
