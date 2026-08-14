"""Privacy-tier ceiling enforcement at the MCP boundary (FEAT-010).

Every read tool accepts a required ``privacy_tier_ceiling`` parameter;
content above the ceiling is omitted or returned as a title-only stub.
The four ceiling values mirror
:class:`creek.classify.privacy_filter.PrivacyTierOverride` so the MCP
surface and ``--include-tier`` stay in lock-step.

Two distinct questions are answered here, both off the same ranking:

- *admission* — :func:`tier_allowed` / :func:`write_tier_allowed`: may this
  content be read (or created) under the caller's ceiling at all? An
  explicit ``unclassified`` tier is *not* open-equivalent here: it ranks
  with ``personal`` (#961), so only a ``personal`` ceiling or broader
  admits it.
- *routing* — :func:`routing_tier`: given that it was admitted, which
  :class:`~creek.models.PrivacyTier` must the LLM call be keyed with so
  :class:`creek.classify.llm.router.ModelRouter` applies the
  ``Intimate``-never-cloud gate (#928)? Every tool that hands content to a
  model derives its tier here rather than deciding for itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.models import PrivacyTier


class TierCeiling(StrEnum):
    """MCP-side ceiling parameter values.

    Ordering: ``OPEN`` is the most restrictive (only ``open`` content is
    visible) and ``ALL`` is the broadest (every tier is visible, including
    ``intimate``). ``unclassified`` is not an ``ALL``-only tier — it ranks
    with ``personal`` (#961), so ``PERSONAL``, ``INTIMATE`` and ``ALL`` all
    admit it and ``OPEN`` alone refuses it.
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


# The one sensitivity ranking in the MCP surface:
# open < personal/unclassified < intimate. Both the admission predicates and
# the routing helpers read it through :func:`tier_sensitivity` so a tier can
# never rank one way for "may I read this?" and another for "where may I send
# it?".
#
# #961, following #876, moved UNCLASSIFIED up to rank with PERSONAL. This is
# the *reader's* caution ordering: an untiered fragment is content nobody has
# vouched for, and ranking it alongside ``open`` exposed a freshly-ingested,
# not-yet-classified corpus to an ``open``-ceiling MCP caller — including a
# remote one. Every fragment carries an explicit ``privacy_tier:
# unclassified`` until ``creek classify`` or ``creek process`` runs, so that
# was the whole vault.
#
# This table now matches :data:`creek.classify.privacy_filter._TIER_RANK`
# deliberately: both answer the *reader's* question, and
# :mod:`creek_mcp.read_gate` exports two canonical gate primitives —
# ``refuse_above_ceiling`` (which reads this table via :func:`tier_allowed`)
# and ``iter_admitted_fragments`` (which reads ``privacy_filter``'s) — so a
# divergence made the two halves of one module disagree about the same
# fragment.
#
# Two OTHER rank tables rank UNCLASSIFIED differently on purpose and must not
# be "unified" with this one:
#
# - :data:`creek.classify.privacy_pass._ESCALATION_RANK` ranks it *below*
#   OPEN (-1), because in an escalate-only merge it means "no claim made"
#   rather than "handle carefully" — it must lose every merge so a real tier
#   can be assigned.
# - :data:`creek.author.checks._TIER_RANK` ranks it *above* INTIMATE (3),
#   because there it is the fail-closed rank for a *cited* fragment in the
#   Writing Desk leak gate.
#
# All four are pinned by
# ``test_unclassified_ranks_differ_by_context_on_purpose`` in
# ``tests/test_mcp_tier_ceiling.py``.
_TIER_RANK = {
    PrivacyTier.OPEN: 0,
    PrivacyTier.UNCLASSIFIED: 1,
    PrivacyTier.PERSONAL: 1,
    PrivacyTier.INTIMATE: 2,
}


# Completeness is the failure mode here, and it is silent: since #1508
# :func:`tier_allowed` refuses an unranked ceiling instead of raising, so
# ``test_ceiling_rank_covers_every_ceiling`` in
# ``tests/test_mcp_tier_ceiling.py`` is what makes a missing entry red.
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
        ``0`` for ``open``; ``1`` for ``personal`` and ``unclassified``
        (#961); ``2`` for ``intimate``. A tier the ranking has never heard
        of is a tier nobody can vouch for, so it ranks *with* ``intimate``
        rather than defaulting to ``0`` and being routed to a cloud
        provider.
    """
    return _TIER_RANK.get(tier, _TIER_RANK[PrivacyTier.INTIMATE])


def _routable_tier(content_tier: PrivacyTier) -> PrivacyTier:
    """Return *content_tier* expressed in the routing vocabulary (#961).

    Normalises ``UNCLASSIFIED`` to ``PERSONAL``: the same rank in
    :data:`_TIER_RANK`, expressed in a word a router understands. Mirrors
    :func:`creek.classify.privacy_filter._effective_tier`, which makes the
    identical substitution for the body-level filter, and is kept separate
    from the admission path for the same reason that helper is kept separate
    from ``tier_of`` — :func:`tier_allowed` must keep ranking the tier that
    is genuinely on the fragment.

    Provider-neutral today, so this is a vocabulary invariant rather than a
    routing behaviour change:
    :meth:`creek.classify.llm.router.ModelRouter._enforce_local_for_intimate`
    gates ``INTIMATE`` alone, so ``open``, ``personal`` and ``unclassified``
    all select the same provider. It is load-bearing nonetheless, because it
    stops an out-of-vocabulary tier reaching ``ModelRouter`` at all: that
    gate is written as "not intimate, or not cloud", so any value it has no
    rule for falls through as the *least* restrictive.
    ``test_routing_tier_answers_only_in_routing_vocabulary`` in
    ``tests/test_mcp_tier_ceiling.py`` pins it.

    Args:
        content_tier: The classified tier of the content being sent.

    Returns:
        ``PERSONAL`` when *content_tier* is ``unclassified``; *content_tier*
        unchanged otherwise.
    """
    if content_tier is PrivacyTier.UNCLASSIFIED:
        return PrivacyTier.PERSONAL
    return content_tier


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
            zero" — it falls back to the ceiling-derived tier. An
            ``unclassified`` tier enters the comparison as ``PERSONAL`` via
            :func:`_routable_tier`, which is where the reasoning lives.

    Returns:
        The more sensitive of the ceiling-derived tier and *content_tier*,
        always within the routing vocabulary — the three tiers in
        :data:`CEILING_ROUTING_TIER` plus ``PERSONAL``, i.e. ``open`` /
        ``personal`` / ``intimate``, never ``unclassified`` (#961). That is
        a vocabulary invariant, not a routing behaviour change: only
        ``INTIMATE`` is gated, so ``open``, ``personal`` and
        ``unclassified`` would all select the same provider today. It is
        load-bearing anyway, because it keeps an out-of-vocabulary tier away
        from :class:`~creek.classify.llm.router.ModelRouter`, whose gate
        treats a tier it has no rule for as the least restrictive. An
        unrecognised ceiling — like an unrecognised tier (see
        :func:`tier_sensitivity`) — fails closed to
        :attr:`~creek.models.PrivacyTier.INTIMATE`, i.e. local-only.
        ``test_routing_tier_answers_only_in_routing_vocabulary`` in
        ``tests/test_mcp_tier_ceiling.py`` pins the invariant.
    """
    ceiling_tier = CEILING_ROUTING_TIER.get(ceiling, PrivacyTier.INTIMATE)
    if content_tier is None:
        return ceiling_tier
    return max(ceiling_tier, _routable_tier(content_tier), key=tier_sensitivity)


def to_privacy_override(ceiling: TierCeiling) -> PrivacyTierOverride:
    """Map a :class:`TierCeiling` to the matching CLI override value.

    Lets tool wrappers feed the ceiling straight into the existing
    privacy-filter machinery so a single source of truth governs which
    fragments are admitted into generation flows.
    """
    return _CEILING_TO_OVERRIDE[ceiling]


def tier_allowed(tier: PrivacyTier, ceiling: TierCeiling) -> bool:
    """Return ``True`` when *tier* is admissible under *ceiling*.

    ``ALL`` admits every tier; other ceilings compare by rank, so
    ``PERSONAL`` admits ``open`` + ``personal`` but rejects ``intimate``,
    and ``open`` admits ``open`` only. ``PERSONAL`` is the lowest ceiling
    that admits ``unclassified`` (#961), which ranks with ``personal``
    rather than with ``open``. The rank comes from
    :func:`tier_sensitivity`, so an unrecognised tier is refused rather
    than raising across the MCP boundary.

    Since #1508 the *ceiling* half of that promise is kept too. It used to
    end on a bare ``_CEILING_RANK[ceiling]`` subscript, which raised
    :class:`KeyError` across the very boundary the tier half was careful
    not to raise across. Both halves now fail closed, and an unrecognised
    ceiling admits **nothing** — not even ``open``. The refusal is spelled
    as an explicit ``None`` check rather than as the ``.get(…, default)``
    :func:`routing_tier` uses, because here the fail-closed answer is not
    another rank but a refusal.

    This is defence in depth, not a live hole:
    :func:`creek_mcp.policy._parse_ceiling` (``creek_mcp/policy.py:151-183``)
    returns ``None`` for any value that does not name a member, before
    :func:`tier_allowed` is ever reached, so no MCP caller can drive the
    branch, and mypy-strict rejects it at every internal call site.

    What keeps the new silent ``False`` from mattering is
    ``test_ceiling_rank_covers_every_ceiling`` in
    ``tests/test_mcp_tier_ceiling.py``. :data:`_CEILING_RANK` is
    referenced nowhere outside this module, so a fifth
    :class:`TierCeiling` member added without a rank entry would
    otherwise be refused everywhere, at every tier, with nothing red.
    """
    if ceiling is TierCeiling.ALL:
        return True
    rank = _CEILING_RANK.get(ceiling)
    if rank is None:
        return False
    return tier_sensitivity(tier) <= rank


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


TIER_REQUIRED_REASON: Final[str] = (
    "tier is required; pass open|personal|intimate explicitly"
)
"""The refusal every write verb owes a caller who omitted ``tier``.

Shared rather than repeated, and it lives beside :func:`refusal_response`
because two independent consumers must read the *same bytes*:

* the three write tools — ``creek.save`` (#1434), ``creek.journal`` and
  ``creek.upload`` (#1494) — each refuse an omitted tier rather than filing
  the caller's content at a defaulted ``open``. A client that learns the
  refusal from one verb must recognise it from the other two, so a single
  literal is what makes the three read alike;
* the ``/v1`` ``ErrorCode`` table in :mod:`creek_mcp.httpapi.journal`, which
  maps this refusal to ``invalid_request``. It keys on this constant rather
  than on a retyped copy of the string, so rewording the sentence moves the
  mapping with it instead of silently dropping the reason into that
  function's fail-closed ``internal_error`` default.
"""


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
