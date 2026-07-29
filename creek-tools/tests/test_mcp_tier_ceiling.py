"""Tests for the MCP privacy-tier ceiling helpers (FEAT-010)."""

from __future__ import annotations

from typing import cast

import pytest

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.models import PrivacyTier
from creek_mcp.tier_ceiling import (
    CEILING_ROUTING_TIER,
    TierCeiling,
    TierCeilingViolationError,
    refusal_response,
    routing_tier,
    tier_allowed,
    tier_sensitivity,
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


# ---------------------------------------------------------------------------
# Routing-tier helpers (#928) — shared by reflect and compile
# ---------------------------------------------------------------------------


def test_ceiling_routing_tier_maps_every_ceiling() -> None:
    """Every ceiling has a routing tier, and ``all`` routes as ``intimate``.

    ``ALL -> INTIMATE`` is the load-bearing row: ``all`` admits intimate
    content by definition, so a call made under it must be routed as though
    intimate content is present whether or not this particular request
    happens to carry any. The equality is against the whole dict (not a
    lookup per row) so a silently *added* ceiling cannot slip through
    unmapped.
    """
    assert CEILING_ROUTING_TIER == {
        TierCeiling.OPEN: PrivacyTier.OPEN,
        TierCeiling.PERSONAL: PrivacyTier.PERSONAL,
        TierCeiling.INTIMATE: PrivacyTier.INTIMATE,
        TierCeiling.ALL: PrivacyTier.INTIMATE,
    }
    assert set(CEILING_ROUTING_TIER) == set(TierCeiling)


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (PrivacyTier.OPEN, 0),
        (PrivacyTier.UNCLASSIFIED, 0),
        (PrivacyTier.PERSONAL, 1),
        (PrivacyTier.INTIMATE, 2),
    ],
)
def test_tier_sensitivity_ranks(tier: PrivacyTier, expected: int) -> None:
    """Sensitivity ranks ``open``/``unclassified`` < ``personal`` < ``intimate``."""
    assert tier_sensitivity(tier) == expected


def test_tier_sensitivity_unknown_tier_fails_closed() -> None:
    """An unranked tier value is treated as the most sensitive one.

    A tier the ranking has never heard of is a tier nobody can vouch for, so
    it must rank *with* ``intimate`` rather than default to 0 and be routed
    to a cloud provider. Asserted against the literal rank as well as
    ``intimate``'s, so lowering the fallback cannot pass by coincidence.
    """
    unknown = cast("PrivacyTier", "not-a-tier")
    assert tier_sensitivity(unknown) == 2
    assert tier_sensitivity(unknown) == tier_sensitivity(PrivacyTier.INTIMATE)


@pytest.mark.parametrize(
    ("ceiling", "content_tier", "expected"),
    [
        (TierCeiling.OPEN, PrivacyTier.OPEN, PrivacyTier.OPEN),
        (TierCeiling.OPEN, PrivacyTier.UNCLASSIFIED, PrivacyTier.OPEN),
        (TierCeiling.OPEN, PrivacyTier.PERSONAL, PrivacyTier.PERSONAL),
        (TierCeiling.OPEN, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
        (TierCeiling.PERSONAL, PrivacyTier.OPEN, PrivacyTier.PERSONAL),
        (TierCeiling.PERSONAL, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
        (TierCeiling.INTIMATE, PrivacyTier.OPEN, PrivacyTier.INTIMATE),
        (TierCeiling.ALL, PrivacyTier.OPEN, PrivacyTier.INTIMATE),
        (TierCeiling.ALL, PrivacyTier.UNCLASSIFIED, PrivacyTier.INTIMATE),
    ],
)
def test_routing_tier_picks_the_more_sensitive_signal(
    ceiling: TierCeiling,
    content_tier: PrivacyTier,
    expected: PrivacyTier,
) -> None:
    """The routing tier is the more sensitive of ceiling-derived and content.

    Both directions are covered: rows where the content is more sensitive
    than the ceiling (an ``intimate`` fragment under ``ceiling=open``) and
    rows where the ceiling is more sensitive than the content (an ``open``
    fragment under ``ceiling=all``). Dropping either term breaks a row.
    """
    assert routing_tier(ceiling, content_tier) is expected


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [
        (TierCeiling.OPEN, PrivacyTier.OPEN),
        (TierCeiling.PERSONAL, PrivacyTier.PERSONAL),
        (TierCeiling.INTIMATE, PrivacyTier.INTIMATE),
        (TierCeiling.ALL, PrivacyTier.INTIMATE),
    ],
)
def test_routing_tier_without_content_tier_uses_the_ceiling(
    ceiling: TierCeiling,
    expected: PrivacyTier,
) -> None:
    """``content_tier=None`` (no classified content) falls back to the ceiling.

    ``None`` is what a caller has when there is nothing classified to
    reconcile against — raw inline ``content`` in ``creek.reflect``. It must
    not be read as "tier zero".
    """
    assert routing_tier(ceiling, None) is expected


def test_routing_tier_unknown_ceiling_fails_closed() -> None:
    """An unrecognised ceiling routes ``intimate`` — local-only."""
    unknown = cast("TierCeiling", "not-a-ceiling")
    assert routing_tier(unknown, None) is PrivacyTier.INTIMATE
    assert routing_tier(unknown, PrivacyTier.OPEN) is PrivacyTier.INTIMATE
