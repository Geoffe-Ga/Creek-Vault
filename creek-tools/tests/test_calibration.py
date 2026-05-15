"""Tests for creek.classify.calibration (FEAT-017b).

Three layers of coverage:

1. **Math** — agreement counts, rates, and the
   :class:`CalibrationFloorBreachError` raise on synthetic data.
2. **Fixture round-trip** — the shipped calibration_set.yaml loads
   into ≥30 well-formed entries.
3. **End-to-end with a deterministic stub** — verifies the calibration
   engine wires into a classifier protocol correctly, without
   network or model calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from creek.classify.calibration import (
    DEFAULT_FLOORS,
    DIMENSIONS,
    CalibrationEntry,
    CalibrationFloorBreachError,
    CalibrationReport,
    DimensionAgreement,
    assert_floors,
    load_fixture,
    run_calibration,
)
from creek.classify.llm import LLMClassificationResult
from creek.models import (
    Authorship,
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

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "classification" / "calibration_set.yaml"
)


# ---- Helpers ----


def _entry(
    fid: str = "cal-test",
    *,
    expected: dict[str, str] | None = None,
) -> CalibrationEntry:
    """Build a minimal CalibrationEntry for math tests."""
    return CalibrationEntry(
        id=fid,
        title="test",
        body="test body",
        expected=expected
        or {
            "frequency": "F3",
            "phase": "rising",
            "mode": "express",
            "orientation": "do",
            "dosage": "medicine",
            "voice_register": "analytical",
        },
    )


def _classified_fragment(
    fid: str,
    *,
    frequency: Frequency = Frequency.F3,
    phase: Phase = Phase.RISING,
    mode: Mode = Mode.EXPRESS,
    orientation: Orientation = Orientation.DO,
    dosage: Dosage = Dosage.MEDICINE,
    voice_register: VoiceRegister | None = VoiceRegister.ANALYTICAL,
) -> Fragment:
    """Build a Fragment carrying the supplied per-dimension labels."""
    return Fragment(
        id=fid,
        title=fid,
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            author=Authorship.SELF,
        ),
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(
            phase=phase,
            mode=mode,
            orientation=orientation,
            dosage=dosage,
        ),
        voice=VoiceClassification(
            voice_register=voice_register,
            confidence=Confidence.FORMING,
        ),
    )


class _FixedStub:
    """Classifier stub whose verdict on every call comes from a callable."""

    def __init__(
        self,
        verdict: Callable[[Fragment], LLMClassificationResult],
    ) -> None:
        self.verdict = verdict
        self.calls = 0

    def classify_with_reasoning(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Return whatever the stored verdict callable produces."""
        del content
        self.calls += 1
        return self.verdict(fragment)


# ---- Math ----


class TestDimensionAgreementRate:
    """Rate is matches/total, with explicit zero-total handling."""

    def test_rate_is_fraction_of_matches(self) -> None:
        """Rate = matches/total."""
        agreement = DimensionAgreement(dimension="frequency", matches=6, total=10)
        assert agreement.rate == 0.6

    def test_rate_zero_when_total_zero(self) -> None:
        """An unscored dimension reports rate 0.0, not division-by-zero."""
        agreement = DimensionAgreement(dimension="orientation", matches=0, total=0)
        assert agreement.rate == 0.0


class TestCalibrationReport:
    """`for_dimension` looks up by name; `render` produces a readable table."""

    def test_for_dimension_returns_match(self) -> None:
        """Lookup returns the named DimensionAgreement."""
        report = CalibrationReport(
            entries=1,
            agreements=(
                DimensionAgreement(dimension="frequency", matches=1, total=1),
                DimensionAgreement(dimension="phase", matches=0, total=1),
            ),
        )
        assert report.for_dimension("phase").matches == 0

    def test_for_dimension_raises_on_missing(self) -> None:
        """An unscored dimension raises KeyError, not silently returns 0."""
        report = CalibrationReport(entries=0, agreements=())
        with pytest.raises(KeyError, match="not scored"):
            report.for_dimension("frequency")

    def test_render_has_header_rows_and_footer(self) -> None:
        """Rendered output names every dimension and the entry count."""
        report = CalibrationReport(
            entries=2,
            agreements=(
                DimensionAgreement(dimension="frequency", matches=1, total=2),
                DimensionAgreement(dimension="phase", matches=2, total=2),
            ),
        )
        text = report.render()
        assert "Dimension" in text
        assert "frequency" in text
        assert "phase" in text
        assert "Entries scored: 2" in text


# ---- Floor assertion ----


class TestAssertFloors:
    """The floor gate raises only when at least one dimension is below floor."""

    def _report_with(self, **rates: float) -> CalibrationReport:
        """Build a report whose dimensions have the requested rates."""
        agreements: list[DimensionAgreement] = []
        for dim in DIMENSIONS:
            rate = rates.get(dim, 1.0)
            # Use total=10 so any 0-1 rate is exactly representable.
            agreements.append(
                DimensionAgreement(dimension=dim, matches=int(rate * 10), total=10),
            )
        return CalibrationReport(entries=10, agreements=tuple(agreements))

    def test_passes_when_every_dimension_at_or_above_floor(self) -> None:
        """A run that meets every floor returns without raising."""
        report = self._report_with(**dict.fromkeys(DIMENSIONS, 1.0))
        assert_floors(report)

    def test_breach_lists_every_failing_dimension(self) -> None:
        """The error message names every floor breach, not just the first."""
        report = self._report_with(frequency=0.1, voice_register=0.1)
        with pytest.raises(CalibrationFloorBreachError) as exc:
            assert_floors(report)
        assert "frequency" in str(exc.value)
        assert "voice_register" in str(exc.value)

    def test_custom_floors_override_defaults(self) -> None:
        """Passing a `floors` arg replaces DEFAULT_FLOORS for the gate."""
        report = self._report_with(frequency=0.5)
        # Below default 0.60 → breach normally.
        with pytest.raises(CalibrationFloorBreachError):
            assert_floors(report)
        # But a custom 0.40 floor passes.
        assert_floors(report, floors={"frequency": 0.40})

    def test_zero_total_dimension_skipped(self) -> None:
        """A dimension with no scored entries does not block the gate."""
        report = CalibrationReport(
            entries=0,
            agreements=(DimensionAgreement(dimension="frequency", matches=0, total=0),),
        )
        # Default floor 0.60, but zero total → no breach.
        assert_floors(report)

    def test_dimension_absent_from_report_is_skipped(self) -> None:
        """A dimension named in floors but not in report is skipped silently."""
        report = CalibrationReport(entries=0, agreements=())
        assert_floors(report, floors={"made_up_dim": 0.99})


# ---- run_calibration end-to-end ----


class TestRunCalibration:
    """`run_calibration` walks the classifier-of-record over each entry."""

    def test_perfect_classifier_hits_every_floor(self) -> None:
        """A classifier that always returns the expected labels passes."""

        def verdict(fragment: Fragment) -> LLMClassificationResult:
            # The entries we use only set the default expectations; build a
            # fragment that matches the entry's expected labels exactly.
            return LLMClassificationResult(
                fragment=_classified_fragment(fragment.id),
                reasoning="perfect",
            )

        stub = _FixedStub(verdict)
        entries = [_entry(f"cal-{i:03d}") for i in range(5)]
        report = run_calibration(stub, entries)
        assert stub.calls == 5
        assert report.entries == 5
        for dim in DIMENSIONS:
            assert report.for_dimension(dim).rate == 1.0
        assert_floors(report)

    def test_unclassified_does_not_match(self) -> None:
        """An ``unclassified`` answer is treated as ``None``, not a match."""

        def verdict(fragment: Fragment) -> LLMClassificationResult:
            # Classifier produces unclassified everywhere; agreement = 0%.
            return LLMClassificationResult(
                fragment=_classified_fragment(
                    fragment.id,
                    frequency=Frequency.UNCLASSIFIED,
                    phase=Phase.UNCLASSIFIED,
                    mode=Mode.UNCLASSIFIED,
                    orientation=Orientation.UNCLASSIFIED,
                    dosage=Dosage.UNCLASSIFIED,
                    voice_register=None,
                ),
                reasoning="",
            )

        report = run_calibration(_FixedStub(verdict), [_entry()])
        for dim in DIMENSIONS:
            assert report.for_dimension(dim).matches == 0
            assert report.for_dimension(dim).total == 1

    def test_partial_expected_block_decrements_total(self) -> None:
        """A fixture entry that omits a dimension is not counted there."""
        entry = _entry(expected={"frequency": "F3"})

        def verdict(fragment: Fragment) -> LLMClassificationResult:
            return LLMClassificationResult(
                fragment=_classified_fragment(fragment.id),
                reasoning="",
            )

        report = run_calibration(_FixedStub(verdict), [entry])
        assert report.for_dimension("frequency").total == 1
        assert report.for_dimension("phase").total == 0


# ---- Fixture load + sanity ----


class TestFixtureLoad:
    """The shipped calibration_set.yaml loads into the expected shape."""

    def test_fixture_exists(self) -> None:
        """The path the FEAT names is checked in."""
        assert _FIXTURE_PATH.is_file(), f"missing fixture at {_FIXTURE_PATH}"

    def test_fixture_carries_at_least_thirty_entries(self) -> None:
        """FEAT-017 acceptance: ≥30 hand-labelled entries shipped."""
        entries = load_fixture(_FIXTURE_PATH)
        assert len(entries) >= 30

    def test_every_entry_has_complete_expected_block(self) -> None:
        """Every entry in the SHIPPED fixture labels every scored dimension.

        Note: this is a quality gate on the shipped fixture only, not an
        engine invariant. The calibration engine (see
        `TestRunCalibration::test_partial_expected_block_decrements_total`)
        supports entries that omit dimensions — that path is for operator-
        supplied fixtures that want to score only a subset. The shipped
        starter set is held to the higher bar so the default
        `--calibrate` invocation exercises every dimension.
        """
        entries = load_fixture(_FIXTURE_PATH)
        for entry in entries:
            assert set(entry.expected.keys()) >= set(DIMENSIONS), (
                f"{entry.id} missing labels: {set(DIMENSIONS) - set(entry.expected)}"
            )

    def test_every_dimension_covered_across_fixture(self) -> None:
        """Each dimension is exercised by at least one entry."""
        entries = load_fixture(_FIXTURE_PATH)
        for dim in DIMENSIONS:
            labels = {e.expected.get(dim) for e in entries if e.expected.get(dim)}
            assert labels, f"no fixture entry exercises {dim}"


class TestFixtureLoadErrors:
    """`load_fixture` rejects malformed input rather than papering over it."""

    def test_non_list_top_level_raises(self, tmp_path: Path) -> None:
        """A YAML scalar / dict at top level is a structural error."""
        path = tmp_path / "bad.yaml"
        path.write_text("frequency: F3\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a YAML list"):
            load_fixture(path)

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        """An entry missing `expected` raises with the index reported."""
        path = tmp_path / "bad.yaml"
        path.write_text(
            "- id: cal-x\n  title: t\n  body: b\n",  # no `expected`
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing keys"):
            load_fixture(path)

    def test_non_dict_entry_raises(self, tmp_path: Path) -> None:
        """A scalar list element raises with its position."""
        path = tmp_path / "bad.yaml"
        path.write_text("- 'not a dict'\n", encoding="utf-8")
        with pytest.raises(ValueError, match="entry #0 is not a dict"):
            load_fixture(path)

    def test_non_dict_expected_block_raises(self, tmp_path: Path) -> None:
        """`expected` must be a mapping, not a list or scalar."""
        path = tmp_path / "bad.yaml"
        path.write_text(
            "- id: x\n  title: t\n  body: b\n  expected: not-a-mapping\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="non-dict 'expected'"):
            load_fixture(path)


# ---- Defaults ----


class TestDefaultFloors:
    """The shipped default floors match the FEAT-017 specification."""

    def test_each_dimension_has_a_floor(self) -> None:
        """Every scored dimension carries an explicit default floor."""
        for dim in DIMENSIONS:
            assert dim in DEFAULT_FLOORS

    def test_voice_register_is_strictest(self) -> None:
        """Voice register is the most stable signal in the FEAT (≥0.75)."""
        assert DEFAULT_FLOORS["voice_register"] == 0.75

    def test_mode_orientation_dosage_share_lenient_floor(self) -> None:
        """The biased dimensions get the looser 0.40 floor (FEAT-017)."""
        assert DEFAULT_FLOORS["mode"] == 0.40
        assert DEFAULT_FLOORS["orientation"] == 0.40
        assert DEFAULT_FLOORS["dosage"] == 0.40


# ---- Internals ----


class TestExtractLabel:
    """`_extract_label` is the dimension dispatch; unknown names must raise."""

    def test_unknown_dimension_raises(self) -> None:
        """An unhandled dimension surfaces loud, not as a silent 0% rate."""
        from creek.classify.calibration import _extract_label

        fragment = _classified_fragment("frag-x")
        with pytest.raises(ValueError, match="unhandled dimension"):
            _extract_label(fragment, "not_a_real_dimension")


class TestDefaultFixturePath:
    """The packaged default fixture must resolve regardless of CWD."""

    def test_default_fixture_resolves_from_arbitrary_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`creek classify --calibrate` must work from any working directory."""
        from creek.cli import _DEFAULT_CALIBRATION_FIXTURE

        monkeypatch.chdir(tmp_path)
        assert _DEFAULT_CALIBRATION_FIXTURE.is_absolute()
        assert _DEFAULT_CALIBRATION_FIXTURE.is_file()
        # Sanity: still points at the shipped fixture.
        assert _DEFAULT_CALIBRATION_FIXTURE.name == "calibration_set.yaml"
