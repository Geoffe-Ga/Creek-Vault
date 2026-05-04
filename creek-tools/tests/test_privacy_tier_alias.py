"""Regression tests for the INC-003 ``PrivacyTier`` rename.

``PrivacyTier.PUBLIC`` was renamed to ``PrivacyTier.OPEN`` (value
``"public"`` → ``"open"``) per ontology §13.2. Old vaults serialised
the tier as ``"public"``; the rename ships a ``_missing_`` hook that
maps the legacy string to :attr:`PrivacyTier.OPEN` while emitting a
:class:`DeprecationWarning` so operators know to migrate.
"""

from __future__ import annotations

import warnings

import pytest

from creek.models import PrivacyTier


def test_open_is_canonical() -> None:
    """``PrivacyTier.OPEN`` is the canonical name + value (openly publishable)."""
    assert PrivacyTier.OPEN.value == "open"
    assert PrivacyTier("open") is PrivacyTier.OPEN


def test_public_alias_returns_open_with_deprecation_warning() -> None:
    """``PrivacyTier("public")`` resolves to OPEN and emits a deprecation warning."""
    with pytest.warns(DeprecationWarning, match="'public' is deprecated"):
        result = PrivacyTier("public")
    assert result is PrivacyTier.OPEN


def test_public_alias_does_not_create_new_enum_value() -> None:
    """The legacy string maps to the existing :attr:`OPEN` member, not a new one."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        result = PrivacyTier("public")
    assert result is PrivacyTier.OPEN
    # PrivacyTier members are exactly: OPEN, PERSONAL, INTIMATE, UNCLASSIFIED.
    assert {m.name for m in PrivacyTier} == {
        "OPEN",
        "PERSONAL",
        "INTIMATE",
        "UNCLASSIFIED",
    }


def test_unknown_tier_still_raises() -> None:
    """Unknown tier values still raise ``ValueError`` — only ``"public"`` is aliased."""
    with pytest.raises(ValueError, match="not a valid PrivacyTier"):
        PrivacyTier("definitely-not-a-tier")


def test_other_existing_values_unchanged() -> None:
    """``PERSONAL``, ``INTIMATE``, and ``UNCLASSIFIED`` round-trip without warnings."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert PrivacyTier("personal") is PrivacyTier.PERSONAL
        assert PrivacyTier("intimate") is PrivacyTier.INTIMATE
        assert PrivacyTier("unclassified") is PrivacyTier.UNCLASSIFIED
