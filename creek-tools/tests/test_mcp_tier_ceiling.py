"""Tests for the MCP privacy-tier ceiling helpers (FEAT-010)."""

from __future__ import annotations

from typing import cast

import pytest

from creek.author.checks import _TIER_RANK as _AUTHOR_LEAK_RANK
from creek.classify.privacy_filter import _TIER_RANK as _READER_RANK
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.classify.privacy_pass import _ESCALATION_RANK
from creek.models import PrivacyTier
from creek_mcp.tier_ceiling import _TIER_RANK as _MCP_RANK
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
        (PrivacyTier.UNCLASSIFIED, TierCeiling.OPEN, False),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.PERSONAL, True),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.INTIMATE, True),
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
    only ``open``.

    Changed by #961: ``unclassified`` at ``ceiling=open`` was ``True`` and is
    now ``False``. An untiered fragment is content nobody has vouched for, and
    every pipeline-written pre-classification fragment carries an *explicit*
    ``privacy_tier: unclassified`` (the ``Fragment`` model default) — so
    admitting it at ``open`` handed a whole freshly-ingested vault to an
    ``open``-ceiling MCP caller. It now ranks with ``personal``, matching
    ``creek.classify.privacy_filter._TIER_RANK``.

    The ``PERSONAL`` and ``INTIMATE`` rows are what pin the new rank from
    *both* sides: a rank of 2 or 3 would also make the ``OPEN`` row false, so
    without them "refused at open" could pass with ``unclassified`` ranked as
    high as ``intimate`` and the ``personal`` ceiling silently broken.
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
        (PrivacyTier.UNCLASSIFIED, 1),
        (PrivacyTier.PERSONAL, 1),
        (PrivacyTier.INTIMATE, 2),
    ],
)
def test_tier_sensitivity_ranks(tier: PrivacyTier, expected: int) -> None:
    """Sensitivity ranks ``open`` < ``personal``/``unclassified`` < ``intimate``.

    Changed by #961: ``unclassified`` ranked 0 (with ``open``) and now ranks 1
    (with ``personal``), so the MCP ceiling agrees with the reader-caution
    ordering in :mod:`creek.classify.privacy_filter`.

    These parameters pin the literal ranks;
    :func:`test_tier_sensitivity_ranks_unclassified_with_personal` pins the
    *relation* separately, because the equivalence with ``personal`` — not the
    number 1 — is the policy.
    """
    assert tier_sensitivity(tier) == expected


def test_tier_sensitivity_ranks_unclassified_with_personal() -> None:
    """``unclassified`` ranks *with* ``personal`` and *above* ``open`` (#961).

    Asserted as a relation rather than against the literal ``1`` deliberately,
    and kept out of the parametrised test above so the failure names the
    property rather than firing once per row. A renumbering that preserved the
    ordering should stay green here; one that quietly split ``unclassified``
    back off from ``personal`` — the #876 caution ordering this issue extended
    to the MCP ceiling — must not.
    """
    assert tier_sensitivity(PrivacyTier.UNCLASSIFIED) == tier_sensitivity(
        PrivacyTier.PERSONAL
    )
    assert tier_sensitivity(PrivacyTier.UNCLASSIFIED) > tier_sensitivity(
        PrivacyTier.OPEN
    )


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
        (TierCeiling.OPEN, PrivacyTier.UNCLASSIFIED, PrivacyTier.PERSONAL),
        (TierCeiling.PERSONAL, PrivacyTier.UNCLASSIFIED, PrivacyTier.PERSONAL),
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

    Changed by #961: ``(OPEN, UNCLASSIFIED)`` expected ``OPEN`` and now
    expects ``PERSONAL``. Under the old rank 0 the ``max`` was a tie the
    ceiling won; ``unclassified`` now outranks ``open``, so it *raises* the
    routing tier — and it is normalised to ``PERSONAL`` on the way out, because
    "unclassified" is not a routing key (see
    :func:`test_routing_tier_answers_only_in_routing_vocabulary`). The added
    ``(PERSONAL, UNCLASSIFIED)`` row pins the outcome where the ceiling and the
    content rank *equal*: the ``max`` tie must still resolve to the routing
    vocabulary, so an implementation that resolved ties toward the content tier
    — and answered ``unclassified`` — fails here as well as at ``open``.
    """
    assert routing_tier(ceiling, content_tier) is expected


@pytest.mark.parametrize("ceiling", list(TierCeiling))
@pytest.mark.parametrize("content_tier", [*PrivacyTier, None])
def test_routing_tier_answers_only_in_routing_vocabulary(
    ceiling: TierCeiling,
    content_tier: PrivacyTier | None,
) -> None:
    """``routing_tier`` never answers ``unclassified``, at any input (#961).

    A *vocabulary* invariant, not a routing change. ``routing_tier`` returns
    the tier an LLM call is **keyed** with, and the three keys that mean
    anything to a router are ``open``, ``personal`` and ``intimate``:
    "unclassified" is the absence of a claim, not a destination. Before #961
    the invariant held for free — at rank 0 ``unclassified`` lost or tied every
    comparison, so the ceiling's own tier always came back. Raising its rank to
    1 would newly let it win under ``ceiling=open``, which is why the
    implementation normalises it to ``PERSONAL`` (the same rank, expressed in
    the routing vocabulary) rather than letting the ``max`` return it verbatim.

    Behaviourally inert today:
    :meth:`creek.classify.llm.router.ModelRouter._enforce_local_for_intimate`
    is the only tier gate and it branches on ``INTIMATE`` alone, so an
    ``unclassified`` key would route exactly as ``personal`` does. Pinned
    anyway, because the next consumer of this value has no reason to expect a
    fourth key, and the exhaustive product is what stops a future tier from
    slipping into the routing vocabulary unexamined.

    Args:
        ceiling: The caller's declared ceiling.
        content_tier: Every ``PrivacyTier``, plus ``None`` for raw
            caller-supplied text that carries no classification.
    """
    expected_vocabulary = set(CEILING_ROUTING_TIER.values()) | {
        PrivacyTier.OPEN,
        PrivacyTier.PERSONAL,
    }
    assert PrivacyTier.UNCLASSIFIED not in expected_vocabulary
    assert routing_tier(ceiling, content_tier) in expected_vocabulary


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


# ---------------------------------------------------------------------------
# The four rank tables for UNCLASSIFIED (#961) — two converge, two must not
#
# #961 closed the split between the two *reader-admission* tables. It did not
# — and must not — unify the other two, which answer different questions with
# the same word. This section is the pin that says so out loud, so a later
# "let's have one ranking" refactor fails here with the reasons attached
# rather than silently breaking the classifier or the leak gate.
# ---------------------------------------------------------------------------


def test_unclassified_ranks_differ_by_context_on_purpose() -> None:
    """Four tables rank ``UNCLASSIFIED`` four ways; only two of them agree (#961).

    Each rank answers a different question about the same word, so "unify
    them" is a refactor that looks like cleanup and is a privacy regression:

    1. :data:`creek.classify.privacy_filter._TIER_RANK` — *reader admission*.
       Rank 1, with ``PERSONAL``. "How cautiously must a reader treat content
       nobody has vouched for?" Cautiously (#876). Unchanged by #961.
    2. :data:`creek_mcp.tier_ceiling._TIER_RANK` — *MCP reader/writer
       admission and routing*. Rank 1, with ``PERSONAL``. The same question as
       (1) asked at the MCP boundary, including for remote callers, so it must
       give the same answer. **This is what #961 changed** (it was 0, with
       ``OPEN``, which made a freshly-ingested, not-yet-classified vault fully
       readable at ``ceiling=open`` — every pipeline-written fragment carries
       an explicit ``privacy_tier: unclassified``, the ``Fragment`` default).
    3. :data:`creek.classify.privacy_pass._ESCALATION_RANK` — the
       *escalate-only merge*. Rank -1, **below** ``OPEN``. Here
       "unclassified" means "no claim made", so it must lose every merge;
       ranking it with ``PERSONAL`` would make the merge refuse to ever assign
       a real tier and re-open the fail-open hole that module closes.
    4. :data:`creek.author.checks._TIER_RANK` — the Writing Desk *leak gate*.
       Rank 3, **above** ``INTIMATE``, the most restrictive. An unclassified
       *cited* fragment must always count as over-tier so its verbatim text
       trips the leak check; lowering it would let uncited-tier content into
       a draft under any contract ceiling.

    Asserted as literals *and* as relations: the literal catches a renumber
    that changes meaning, the relation catches a renumber that preserves the
    ordering (which should stay green) versus one that moves ``UNCLASSIFIED``
    across a neighbour (which must not).
    """
    # (1) and (2) — the two reader-admission tables, now in agreement.
    assert _READER_RANK[PrivacyTier.UNCLASSIFIED] == 1
    assert _READER_RANK[PrivacyTier.UNCLASSIFIED] == _READER_RANK[PrivacyTier.PERSONAL]
    assert _MCP_RANK[PrivacyTier.UNCLASSIFIED] == 1
    assert _MCP_RANK[PrivacyTier.UNCLASSIFIED] == _MCP_RANK[PrivacyTier.PERSONAL]
    assert _MCP_RANK[PrivacyTier.UNCLASSIFIED] > _MCP_RANK[PrivacyTier.OPEN]

    # (3) — the escalate-only merge must keep ranking it BELOW open.
    assert _ESCALATION_RANK[PrivacyTier.UNCLASSIFIED] == -1
    assert (
        _ESCALATION_RANK[PrivacyTier.UNCLASSIFIED] < _ESCALATION_RANK[PrivacyTier.OPEN]
    )

    # (4) — the leak gate must keep ranking it ABOVE intimate.
    assert _AUTHOR_LEAK_RANK[PrivacyTier.UNCLASSIFIED] == 3
    assert (
        _AUTHOR_LEAK_RANK[PrivacyTier.UNCLASSIFIED]
        > _AUTHOR_LEAK_RANK[PrivacyTier.INTIMATE]
    )


def test_privacy_filter_and_mcp_tier_sensitivity_agree_on_every_tier() -> None:
    """The two reader-side ``tier_sensitivity`` tables agree, tier for tier (#962).

    #962 moves ``fragment_tier`` and ``max_source_tier`` out of
    :mod:`creek.classify.privacy_filter` into
    :mod:`creek.classify.privacy_filter`, so ``creek.compile.engine`` can
    derive its own routing tier without importing the MCP package.
    ``max_source_tier`` reduces with ``max(..., key=tier_sensitivity)``, so
    after the move MCP **routing** ranks tiers through *creek's* table while
    MCP **admission** (:func:`tier_allowed` / ``write_tier_allowed``) still
    ranks them through the MCP's.

    Those two rankings were incidentally equal before and are load-bearing
    now. If they diverged, ``creek_mcp.tools.compile`` could admit a batch
    under one ordering and then route it as though its most sensitive member
    were a different tier — precisely the "admission and routing disagree
    about the same fragment" failure the shared-loader design exists to
    prevent.

    **This does not license merging the tables.** Two of the four rankings
    pinned by :func:`test_unclassified_ranks_differ_by_context_on_purpose`
    rank ``UNCLASSIFIED`` differently *on purpose* — the escalate-only merge
    below ``OPEN``, the Writing Desk leak gate above ``INTIMATE``. Agreement
    is asserted here for the two reader-side tables only, and asserting it
    is exactly what lets them stay two deliberate declarations rather than
    collapsing into one unexamined import.

    The absolute ranks are pinned alongside the equality: two tables
    agreeing on a *wrong* ranking would satisfy equality on its own.
    """
    # Imported locally rather than at module scope: this module's top-level
    # ``tier_sensitivity`` is the MCP one that ~a dozen tests here call by
    # bare name, and adding a second import of the same name — even aliased
    # — invites a later edit to shadow it silently.
    from creek.classify.privacy_filter import (
        tier_sensitivity as creek_tier_sensitivity,
    )

    expected = {
        PrivacyTier.OPEN: 0,
        PrivacyTier.UNCLASSIFIED: 1,
        PrivacyTier.PERSONAL: 1,
        PrivacyTier.INTIMATE: 2,
    }
    # Every enum member, not a hand-picked subset: a tier added later with no
    # entry in one of the two tables has to fail here.
    assert set(PrivacyTier) == set(expected)
    assert {tier: creek_tier_sensitivity(tier) for tier in PrivacyTier} == expected
    assert {tier: tier_sensitivity(tier) for tier in PrivacyTier} == expected

    # Both must fail closed identically on a tier neither table has heard of.
    # A divergence there would route an out-of-vocabulary tier to a cloud
    # provider on one side while refusing to admit it on the other.
    unknown = cast("PrivacyTier", "not-a-tier")
    assert creek_tier_sensitivity(unknown) == tier_sensitivity(unknown)
    assert creek_tier_sensitivity(unknown) == expected[PrivacyTier.INTIMATE]
