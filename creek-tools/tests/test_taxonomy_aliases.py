"""Regression tests for INC-019 one-release migration aliases.

INC-019 reconciles spec/implementation drift on three axes — phase,
mode, and frequency — by bringing ``creek-tools`` to the canonical
ontology vocabulary. Old vaults / hand-edited frontmatter may still
serialise the drifted strings (``origins``, ``cresting``, ``solo``,
``amplitude``, etc.). For one release we accept those values on input,
silently map them to the canonical names, and emit a
:class:`DeprecationWarning` so the operator knows to migrate.

The pattern mirrors INC-003's ``PrivacyTier("public")`` alias — see
``tests/test_privacy_tier_alias.py``.
"""

from __future__ import annotations

import warnings

import pytest

from creek.models import Frequency, Mode, Phase

PHASE_ALIASES = {
    "origins": Phase.RISING,
    "cresting": Phase.WITHDRAWAL,
    "receding": Phase.DIMINISHING,
    "composting": Phase.RESTORATION,
}

MODE_ALIASES = {
    "solo": Mode.INHABIT,
    "dialogue": Mode.EXPRESS,
    "reflective": Mode.INTEGRATE,
    "analytic": Mode.COLLABORATE,
}

FREQUENCY_ALIASES = {
    "amplitude": Frequency.F1,
    "pitch": Frequency.F2,
}


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    list(PHASE_ALIASES.items()),
)
def test_phase_legacy_value_maps_to_canonical_with_warning(
    legacy: str,
    canonical: Phase,
) -> None:
    """Drifted phase strings resolve to their canonical enum member."""
    with pytest.warns(DeprecationWarning, match="INC-019"):
        result = Phase(legacy)
    assert result is canonical


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    list(MODE_ALIASES.items()),
)
def test_mode_legacy_value_maps_to_canonical_with_warning(
    legacy: str,
    canonical: Mode,
) -> None:
    """Drifted mode strings resolve to their canonical enum member."""
    with pytest.warns(DeprecationWarning, match="INC-019"):
        result = Mode(legacy)
    assert result is canonical


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    list(FREQUENCY_ALIASES.items()),
)
def test_frequency_legacy_value_maps_to_canonical_with_warning(
    legacy: str,
    canonical: Frequency,
) -> None:
    """Drifted frequency strings resolve to the matching F-code."""
    with pytest.warns(DeprecationWarning, match="INC-019"):
        result = Frequency(legacy)
    assert result is canonical


def test_unknown_phase_still_raises() -> None:
    """Unknown phase strings continue to raise ``ValueError``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="not a valid Phase"):
            Phase("definitely-not-a-phase")


def test_unknown_mode_still_raises() -> None:
    """Unknown mode strings continue to raise ``ValueError``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="not a valid Mode"):
            Mode("definitely-not-a-mode")


def test_unknown_frequency_still_raises() -> None:
    """Unknown frequency strings continue to raise ``ValueError``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="not a valid Frequency"):
            Frequency("definitely-not-a-frequency")


def test_aliases_do_not_create_new_enum_members() -> None:
    """Aliases route to existing members, never minting new ones."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for legacy in PHASE_ALIASES:
            Phase(legacy)
        for legacy in MODE_ALIASES:
            Mode(legacy)
        for legacy in FREQUENCY_ALIASES:
            Frequency(legacy)
    # Only the canonical six phases plus ``unclassified`` exist.
    assert len(list(Phase)) == 7
    # Only the canonical five modes plus ``unclassified`` exist.
    assert len(list(Mode)) == 6
    # Only F1..F10 plus ``unclassified`` exist.
    assert len(list(Frequency)) == 11


def test_canonical_values_round_trip_without_warnings() -> None:
    """Canonical values round-trip silently — no deprecation noise."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert Phase("rising") is Phase.RISING
        assert Phase("bottoming_out") is Phase.BOTTOMING_OUT
        assert Mode("inhabit") is Mode.INHABIT
        assert Mode("absorb") is Mode.ABSORB
        assert Frequency("F1") is Frequency.F1
        assert Frequency("F10") is Frequency.F10
