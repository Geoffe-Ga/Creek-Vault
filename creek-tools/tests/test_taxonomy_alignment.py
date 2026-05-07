"""Regression tests for INC-019 wavelength taxonomy alignment.

These tests pin the canonical phase, mode, and frequency vocabulary
defined in ``00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md``
and assert that ``creek.models`` agrees with that spec verbatim.

The canonical lists live in ``tests/fixtures/canonical_taxonomy.yaml``
so the test fails when *either* the spec or the model drifts. If the
ontology spec ever revises a name, both the fixture and the model
must be updated together — the test makes that requirement explicit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from creek.models import Frequency, Mode, Phase

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "canonical_taxonomy.yaml"


@pytest.fixture(scope="module")
def canonical() -> dict[str, Any]:
    """Load the canonical taxonomy fixture once per test module."""
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict)
    return loaded


def test_phase_enum_matches_canonical_list_in_order(
    canonical: dict[str, Any],
) -> None:
    """``Phase`` exposes the spec's six phases in canonical narrative order."""
    expected = canonical["phases"]["canonical"]
    actual = [p.value for p in Phase if p is not Phase.UNCLASSIFIED]
    assert actual == expected


def test_mode_enum_matches_canonical_list_in_order(
    canonical: dict[str, Any],
) -> None:
    """``Mode`` exposes the spec's five modes in canonical order."""
    expected = canonical["modes"]["canonical"]
    actual = [m.value for m in Mode if m is not Mode.UNCLASSIFIED]
    assert actual == expected


def test_frequency_enum_matches_canonical_codes(
    canonical: dict[str, Any],
) -> None:
    """``Frequency`` exposes F1..F10 in spec order (plus ``unclassified``)."""
    expected = canonical["frequencies"]["canonical_codes"]
    actual = [f.value for f in Frequency if f is not Frequency.UNCLASSIFIED]
    assert actual == expected


def test_phase_enum_has_no_drift_values() -> None:
    """No drift phase names (``origins``, ``cresting``, ``receding``,
    ``composting``) are first-class enum members."""
    drift = {"origins", "cresting", "receding", "composting"}
    enum_values = {p.value for p in Phase}
    assert drift.isdisjoint(enum_values)


def test_mode_enum_has_no_drift_values() -> None:
    """No drift mode names (``solo``, ``dialogue``, ``reflective``,
    ``analytic``) are first-class enum members."""
    drift = {"solo", "dialogue", "reflective", "analytic"}
    enum_values = {m.value for m in Mode}
    assert drift.isdisjoint(enum_values)


def test_frequency_enum_has_no_wave_physics_drift() -> None:
    """No drift frequency names (``amplitude``, ``pitch``) are
    first-class enum members."""
    drift = {"amplitude", "pitch"}
    enum_values = {f.value for f in Frequency}
    assert drift.isdisjoint(enum_values)


def test_canonical_fixture_lists_have_expected_lengths(
    canonical: dict[str, Any],
) -> None:
    """The fixture itself exposes 6 phases, 5 modes, and 10 frequencies."""
    assert len(canonical["phases"]["canonical"]) == 6
    assert len(canonical["modes"]["canonical"]) == 5
    assert len(canonical["frequencies"]["canonical_codes"]) == 10
