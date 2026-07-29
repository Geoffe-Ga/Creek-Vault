"""Privacy-tier ceiling enforcement at the MCP boundary (FEAT-010).

Every read tool accepts a required ``privacy_tier_ceiling`` parameter;
content above the ceiling is omitted or returned as a title-only stub.
The four ceiling values mirror
:class:`creek.classify.privacy_filter.PrivacyTierOverride` so the MCP
surface and ``--include-tier`` stay in lock-step.

Two distinct questions are answered here, both off the same ranking:

- *admission* — :func:`tier_allowed` / :func:`write_tier_allowed`: may this
  content be read (or created) under the caller's ceiling at all?
- *routing* — :func:`routing_tier`: given that it was admitted, which
  :class:`~creek.models.PrivacyTier` must the LLM call be keyed with so
  :class:`creek.classify.llm.router.ModelRouter` applies the
  ``Intimate``-never-cloud gate (#928)? Every tool that hands content to a
  model derives its tier here rather than deciding for itself.
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


# The one sensitivity ranking in the MCP surface: open/unclassified <
# personal < intimate. Both the admission predicates and the routing helpers
# read it through :func:`tier_sensitivity` so a tier can never rank one way
# for "may I read this?" and another for "where may I send it?".
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


# The most sensitive tier each ceiling admits, i.e. the tier a call made under
# it must be *routed* as. ``ALL`` admits intimate content by definition, so a
# call under it routes INTIMATE whether or not this particular request happens
# to carry any.
CEILING_ROUTING_TIER: dict[TierCeiling, PrivacyTier] = {
    TierCeiling.OPEN: PrivacyTier.OPEN,
    TierCeiling.PERSONAL: PrivacyTier.PERSONAL,
    TierCeiling.INTIMATE: PrivacyTier.INTIMATE,
    TierCeiling.ALL: PrivacyTier.INTIMATE,
}


def tier_sensitivity(tier: PrivacyTier) -> int:
    """Return the routing/admission rank of *tier*, failing closed.

    Args:
        tier: The tier to rank.

    Returns:
        ``0`` for ``open`` and ``unclassified``, ``1`` for ``personal``,
        ``2`` for ``intimate``. A tier the ranking has never heard of is a
        tier nobody can vouch for, so it ranks *with* ``intimate`` rather
        than defaulting to ``0`` and being routed to a cloud provider.
    """
    return _TIER_RANK.get(tier, _TIER_RANK[PrivacyTier.INTIMATE])


def routing_tier(ceiling: TierCeiling, content_tier: PrivacyTier | None) -> PrivacyTier:
    """Return the tier an LLM call must be keyed with (#928).

    The router's cloud gate keys on :class:`~creek.models.PrivacyTier`, never
    on :class:`TierCeiling`, so every tool that hands content to a model
    reconciles the two available signals here by taking the **more
    sensitive**: the content's own classification, and the ceiling the caller
    declared (itself a statement about what the call is permitted to reach).

    Taking the maximum is what makes the result uncheatable from the outside:
    a caller can neither declare a low ceiling to win cloud routing for
    intimate content, nor supply low-tier content to win it under a broad
    ceiling.

    Args:
        ceiling: The caller's declared ceiling.
        content_tier: The classified tier of the content being sent, or
            ``None`` when there is nothing classified to reconcile against
            (raw caller-supplied text). ``None`` must not be read as "tier
            zero" — it falls back to the ceiling-derived tier.

    Returns:
        The more sensitive of the ceiling-derived tier and *content_tier*.
        An unrecognised ceiling — like an unrecognised tier (see
        :func:`tier_sensitivity`) — fails closed to
        :attr:`~creek.models.PrivacyTier.INTIMATE`, i.e. local-only.
    """
    ceiling_tier = CEILING_ROUTING_TIER.get(ceiling, PrivacyTier.INTIMATE)
    if content_tier is None:
        return ceiling_tier
    return max(ceiling_tier, content_tier, key=tier_sensitivity)


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
    ``personal`` but rejects ``intimate``. The rank comes from
    :func:`tier_sensitivity`, so an unrecognised tier is refused rather
    than raising across the MCP boundary.
    """
    if ceiling is TierCeiling.ALL:
        return True
    return tier_sensitivity(tier) <= _CEILING_RANK[ceiling]


def write_tier_allowed(write_tier: PrivacyTier, ceiling: TierCeiling) -> bool:
    """Return ``True`` when a *write_tier*-producing call is admissible.

    Mirrors :func:`tier_allowed` but expresses the FEAT-011 write-side
    rule explicitly: a write tool that *would create* content at tier
    ``T`` requires the caller's ``privacy_tier_ceiling`` to admit ``T``.
    A caller with ``ceiling=open`` cannot create ``personal`` /
    ``intimate`` content via MCP; the write must be refused rather than
    silently downgraded.
    """
    return tier_allowed(write_tier, ceiling)


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
