"""Privacy-tier ceiling enforcement at the MCP boundary (FEAT-010).

Every read tool accepts a required ``privacy_tier_ceiling`` parameter;
content above the ceiling is omitted or returned as a title-only stub.
The four ceiling values mirror
:class:`creek.classify.privacy_filter.PrivacyTierOverride` so the MCP
surface and ``--include-tier`` stay in lock-step.
"""

from __future__ import annotations

from enum import StrEnum

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.models import PrivacyTier


class TierCeiling(StrEnum):
    """MCP-side ceiling parameter values.

    Ordering: ``OPEN`` is the most restrictive (only ``open`` content is
    visible) and ``ALL`` is the broadest (every tier, including
    ``unclassified``, is visible).
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    ALL = "all"


class TierCeilingViolationError(Exception):
    """Raised when a tool would return content above the ceiling.

    Tool wrappers convert this into a structured ``"refused"`` response
    rather than a transport-level error; the exception keeps the refusal
    path in one place so no individual tool can silently leak content
    above the ceiling.
    """


_CEILING_TO_OVERRIDE = {
    TierCeiling.OPEN: PrivacyTierOverride.OPEN,
    TierCeiling.PERSONAL: PrivacyTierOverride.PERSONAL,
    TierCeiling.INTIMATE: PrivacyTierOverride.INTIMATE,
    TierCeiling.ALL: PrivacyTierOverride.ALL,
}


_TIER_RANK = {
    PrivacyTier.OPEN: 0,
    PrivacyTier.UNCLASSIFIED: 0,
    PrivacyTier.PERSONAL: 1,
    PrivacyTier.INTIMATE: 2,
}


_CEILING_RANK = {
    TierCeiling.OPEN: 0,
    TierCeiling.PERSONAL: 1,
    TierCeiling.INTIMATE: 2,
    TierCeiling.ALL: 3,
}


def to_privacy_override(ceiling: TierCeiling) -> PrivacyTierOverride:
    """Map a :class:`TierCeiling` to the matching CLI override value.

    Lets tool wrappers feed the ceiling straight into the existing
    privacy-filter machinery so a single source of truth governs which
    fragments are admitted into generation flows.
    """
    return _CEILING_TO_OVERRIDE[ceiling]


def tier_allowed(tier: PrivacyTier, ceiling: TierCeiling) -> bool:
    """Return ``True`` when *tier* is admissible under *ceiling*.

    ``ALL`` admits every tier (including ``unclassified``); other
    ceilings compare by rank so ``PERSONAL`` admits ``open`` +
    ``personal`` but rejects ``intimate``.
    """
    if ceiling is TierCeiling.ALL:
        return True
    return _TIER_RANK[tier] <= _CEILING_RANK[ceiling]


def refusal_response(
    *,
    tool: str,
    ceiling: TierCeiling,
    reason: str,
) -> dict[str, object]:
    """Build the canonical ``"refused"`` payload for tier violations.

    Tool wrappers return this dict verbatim so MCP clients can rely on a
    stable shape: ``status: "refused"``, an echo of the offending tool
    + ceiling, and a human-readable ``reason``.
    """
    return {
        "status": "refused",
        "tool": tool,
        "tier_ceiling": ceiling.value,
        "reason": reason,
    }
