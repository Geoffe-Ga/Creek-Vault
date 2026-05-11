"""Tests for the MCP privacy-tier ceiling helpers (FEAT-010)."""

from __future__ import annotations

import pytest

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.models import PrivacyTier
from creek_mcp.tier_ceiling import (
    TierCeiling,
    TierCeilingViolationError,
    refusal_response,
    tier_allowed,
    to_privacy_override,
)


def test_tier_ceiling_values_match_cli_override() -> None:
    """Ceiling values mirror the existing CLI ``--include-tier`` enum."""
    assert {t.value for t in TierCeiling} == {t.value for t in PrivacyTierOverride}


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [
        (TierCeiling.OPEN, PrivacyTierOverride.OPEN),
        (TierCeiling.PERSONAL, PrivacyTierOverride.PERSONAL),
        (TierCeiling.INTIMATE, PrivacyTierOverride.INTIMATE),
        (TierCeiling.ALL, PrivacyTierOverride.ALL),
    ],
)
def test_to_privacy_override_maps_one_to_one(
    ceiling: TierCeiling,
    expected: PrivacyTierOverride,
) -> None:
    """Every ceiling resolves to the matching CLI override."""
    assert to_privacy_override(ceiling) is expected


@pytest.mark.parametrize(
    ("tier", "ceiling", "expected"),
    [
        (PrivacyTier.OPEN, TierCeiling.OPEN, True),
        (PrivacyTier.PERSONAL, TierCeiling.OPEN, False),
        (PrivacyTier.INTIMATE, TierCeiling.OPEN, False),
        (PrivacyTier.OPEN, TierCeiling.PERSONAL, True),
        (PrivacyTier.PERSONAL, TierCeiling.PERSONAL, True),
        (PrivacyTier.INTIMATE, TierCeiling.PERSONAL, False),
        (PrivacyTier.INTIMATE, TierCeiling.INTIMATE, True),
        (PrivacyTier.INTIMATE, TierCeiling.ALL, True),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.OPEN, True),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.ALL, True),
    ],
)
def test_tier_allowed_matrix(
    tier: PrivacyTier,
    ceiling: TierCeiling,
    expected: bool,
) -> None:
    """``tier_allowed`` rejects content above the ceiling.

    ``all`` admits every tier including ``unclassified``; ``open`` admits
    only ``open`` and ``unclassified``.
    """
    assert tier_allowed(tier, ceiling) is expected


def test_refusal_response_shape() -> None:
    """Refusals have a stable, MCP-friendly dict shape."""
    response = refusal_response(
        tool="creek.draft",
        ceiling=TierCeiling.OPEN,
        reason="intimate content requested",
    )
    assert response == {
        "status": "refused",
        "tool": "creek.draft",
        "tier_ceiling": "open",
        "reason": "intimate content requested",
    }


def test_tier_ceiling_violation_is_exception() -> None:
    """The violation error is raisable and catchable in tool wrappers."""
    with pytest.raises(TierCeilingViolationError):
        raise TierCeilingViolationError("test")
