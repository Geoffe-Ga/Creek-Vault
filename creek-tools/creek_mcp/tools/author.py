"""``creek.author`` MCP tool — author via the real Writing Desk (FEAT-041 #460).

A thin adapter: it validates the request, records the audit entry, and
delegates to ``creek.author.run_author`` (or ``plan_author`` for a dry run) —
no desk logic lives in the MCP layer (SPEC §4.3: both the CLI and MCP surfaces
call the same desk). The response carries the draft body, the reflection
verdict, per-claim provenance, and the cited ``claims`` (each with its
``source_fragments``) — **unless** the desk's HARD privacy check condemned the
draft, in which case the verb answers ``status: "refused"`` and withholds all
three content keys (#1353). Returning a ``privacy_compliance`` finding
*alongside* the protected text it just caught hands the caller the very breach
the gate detected; the finding travels, the corpus does not. Existing MCP verbs
are untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

from creek.author import plan_author, require_supported_medium, run_author
from creek.author.checks import ZERO_EVIDENCE_WARNING, has_zero_evidence
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author import AuthoredDraft, ReflectionFinding
    from creek.author.client import AuthorLLMClient
    from creek.author.models import FindingDimension
    from creek.models import PrivacyTier

TOOL_NAME = "creek.author"


# The one reflection dimension whose presence turns a finished draft into a
# refusal (#1353). Annotated with :data:`~creek.author.models.FindingDimension`
# — the ``Literal`` that already contains this exact string — so strict mypy
# rejects a typo here instead of leaving a filter that silently matches nothing
# forever, which would restore the leak without failing anything.
#
# The literal is deliberately re-stated rather than imported from whatever
# module happens to construct the finding (today, the body of
# :func:`creek.author.checks.check_privacy_compliance`) or from the CLI's own
# module-local twin (``creek/cli.py:4954``, landed by #1352): an MCP tool
# reaching into another surface's private internals is a worse coupling than
# one duplicated string that ``FindingDimension`` already type-checks.
# Promoting a single shared public constant next to the check itself is the
# right eventual cleanup and is tracked as issue #1362, not done here.
_PRIVACY_DIMENSION: Final[FindingDimension] = "privacy_compliance"


# The refusal's ``reason``: fixed prose plus a count, and nothing else. No
# fragment id, no draft text, no finding message is interpolated — a refusal
# that quotes the corpus to explain itself has not refused anything. The
# offending ids and tiers ride in ``privacy_findings`` instead, whose messages
# are corpus-free by construction (``creek/author/checks.py:342-346``: fragment
# id and tier only).
_PRIVACY_REFUSAL_REASON: Final[str] = (
    "The Writing Desk's privacy gate condemned this draft "
    "({count} privacy_compliance finding(s)): it reproduces protected text "
    "from a cited fragment held above the medium contract's default privacy "
    "tier. The body, claims and provenance are withheld; see "
    "privacy_findings for the offending fragments and their tiers."
)


class AuthorLLMFactory(Protocol):
    """Tier-keyed builder for the Writing Desk's voice client.

    ``factory(tier)`` returns the client the voice node speaks through for a
    run whose evidence carries *tier* content, or ``None`` to keep the desk's
    deterministic rendering. The production factory resolves through
    :class:`creek.classify.llm.router.ModelRouter`, so Intimate content is
    redirected to a local provider by the chokepoint (#658/#661); this module
    never picks a provider itself.

    The sibling of :class:`creek_mcp.tools.draft.DraftLLMFactory` and
    :class:`creek_mcp.tools.compile.CompileLLMFactory`, and added for the same
    reason (#1254): with no seam, ``creek.author`` answered ``status: ok``
    carrying a deterministically-stubbed body that no hermetic test could tell
    apart from a live one — the #460/#649/#658 signature.

    Invoked lazily, inside the run, so an unconfigured provider fails only an
    actual authoring call and never server startup.
    """

    def __call__(self, tier: PrivacyTier | None) -> AuthorLLMClient | None: ...


def _error_response(
    reason: str,
    *,
    tier_ceiling: TierCeiling,
    dry_run: bool,
) -> dict[str, Any]:
    """Render a structured error envelope (used on every failure path)."""
    return {
        "status": "error",
        "tool": TOOL_NAME,
        "tier_ceiling": tier_ceiling.value,
        "tier_ceiling_enforced": False,
        "dry_run": dry_run,
        "reason": reason,
    }


def _privacy_refusal_response(
    draft: AuthoredDraft,
    leaks: list[ReflectionFinding],
    *,
    tier_ceiling: TierCeiling,
) -> dict[str, Any]:
    """Render the refusal envelope for a draft the privacy gate condemned.

    Built on :func:`creek_mcp.tier_ceiling.refusal_response` so this is the same
    ``status: "refused"`` shape ``creek.link``, ``creek.report``,
    ``creek.state.read``, ``creek.journal`` and ``creek.upload`` already emit,
    rather than a sixth hand-rolled refusal: every consumer's ``status != "ok"``
    branch then handles it for free. Not ``status: "error"`` — nothing
    malfunctioned. :func:`_error_response` owns the malformed/blew-up path, and
    collapsing a successful refusal into it would make a provider outage
    indistinguishable from a privacy stop.

    ``body``, ``claims`` and ``provenance`` are *absent*, not emptied. A
    falsy-but-present ``body`` is exactly the shape that lets a caller print
    nothing and believe it published a draft; with the key gone,
    ``response["body"]`` raises at the moment of the refusal instead.

    Every value is copied field by field on purpose. Never reach for
    ``draft.model_dump()`` here: :class:`~creek.author.models.AuthoredDraft`
    carries a ``rendered_text`` ``@computed_field`` that aliases ``body`` and
    *is* included in dumps, so any dump smuggles the condemned prose back onto
    the wire under a second name.

    Args:
        draft: The condemned draft. Only its non-content metadata is echoed.
        leaks: Its findings, already filtered to :data:`_PRIVACY_DIMENSION` by
            :func:`_draft_response` — see there for why the filter must happen
            before anything is serialised.
        tier_ceiling: The caller's ceiling, echoed back unchanged.

    Returns:
        The canonical refusal keys plus this verb's own non-content metadata and
        the privacy findings.
    """
    return {
        **refusal_response(
            tool=TOOL_NAME,
            ceiling=tier_ceiling,
            reason=_PRIVACY_REFUSAL_REASON.format(count=len(leaks)),
        ),
        # "refused" with "enforced: True" is not a contradiction. This flag is a
        # claim about the #660 retrieval filter, which ran and did its job: at
        # ceiling=all the caller *is* admitted to intimate content, so no
        # ceiling was violated. The breach is against a different gate — the
        # medium contract's `default_privacy_tier` (`open` in all six shipped
        # templates) — which is exactly why the leak survives at `all` and why
        # every ceiling=open probe missed it.
        "tier_ceiling_enforced": True,
        "dry_run": False,
        "medium": draft.medium,
        "query": draft.query,
        "verdict": draft.verdict,
        "rounds": draft.rounds,
        "privacy_findings": [
            {
                "dimension": finding.dimension,
                "severity": finding.severity,
                "message": finding.message,
            }
            for finding in leaks
        ],
    }


def _draft_response(
    draft: AuthoredDraft,
    *,
    tier_ceiling: TierCeiling,
) -> dict[str, Any]:
    """Render an :class:`AuthoredDraft` into the MCP success envelope.

    A draft carrying any :data:`_PRIVACY_DIMENSION` finding is refused instead
    (#1353). The desk's HARD privacy check fires when the drafted prose
    reproduces a cited fragment's protected text verbatim, and the success
    envelope would then carry that text three times over — in ``body``, in
    ``claims[*].claim`` and in ``provenance[*].claim_excerpt`` — under
    ``status: "ok"``.

    The trigger is the finding *dimension*, never ``verdict == "ESCALATE"``:
    escalation is routine here (an empty vault escalates, and any unresolved
    soft finding escalates once the round budget runs out —
    ``creek/author/conductor.py:430-436``), so gating on the verdict would
    withhold ordinary drafts. It is not conditioned on severity either:
    ``check_privacy_compliance`` only emits ``HIGH`` today, and a
    dimension-only trigger stays correct if a future ``MID`` privacy finding
    appears.

    The findings are filtered to that one dimension *here*, before anything
    reaches an envelope. Echoing all of ``draft.findings`` would undo the
    scrub, because two sibling checks interpolate corpus text into their own
    messages: ``biographical_grounding`` embeds a sentence lifted from the
    drafted body (``creek/author/checks.py:176``) and
    ``attribution_correctness`` embeds the borrowed claim's text — the cited
    fragment's title, on the deterministic path (``checks.py:536``).
    Hence the refusal key is ``privacy_findings`` and not ``findings`` — a name
    nobody can "complete" by widening the filter without also having to rename
    it.

    Args:
        draft: The desk's finished draft.
        tier_ceiling: The caller's ceiling, echoed back unchanged.

    Returns:
        The success envelope, or — when the draft carries a privacy finding —
        the refusal envelope from :func:`_privacy_refusal_response`.
    """
    leaks = [leak for leak in draft.findings if leak.dimension == _PRIVACY_DIMENSION]
    if leaks:
        return _privacy_refusal_response(draft, leaks, tier_ceiling=tier_ceiling)
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": tier_ceiling.value,
        # #660: the specialists tier-filter retrieval against the ceiling, so
        # the echo now reflects real enforcement.
        "tier_ceiling_enforced": True,
        "dry_run": False,
        "medium": draft.medium,
        "query": draft.query,
        "body": draft.body,
        "verdict": draft.verdict,
        "rounds": draft.rounds,
        "provenance": [entry.model_dump(mode="json") for entry in draft.provenance],
        "claims": [
            {"claim": entry.claim_excerpt, "source_fragments": entry.fragment_ids}
            for entry in draft.provenance
        ],
        # #1261: the same signal the CLI prints, carried on the wire so an MCP
        # consumer is not the one surface left guessing why a draft cites
        # nothing. A LIST rather than a bool: it extends without a contract
        # change if a second advisory is ever added, and no existing test
        # makes an exact-key claim on this envelope.
        "warnings": ([ZERO_EVIDENCE_WARNING] if has_zero_evidence(draft) else []),
    }


def author_tool(
    *,
    vault_path: Path,
    query: str,
    llm_factory: AuthorLLMFactory | None = None,
    medium: str = "research",
    max_rounds: int | None = None,
    dry_run: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Author *query* via the real Writing Desk and return the result.

    Mirrors the ``creek author`` CLI args. Delegates to ``run_author`` (or
    ``plan_author`` when ``dry_run`` is set); the verb adds no desk logic. The
    caller's privacy ceiling is recorded **and enforced** (#660): it is
    converted to a :class:`~creek.classify.privacy_filter.PrivacyTierOverride`
    and threaded to the specialists, which exclude above-ceiling fragments from
    the evidence.

    Args:
        vault_path: The vault to author from.
        query: The user query to author about.
        llm_factory: Tier-keyed builder for the desk's voice client (#1254).
            ``None`` — the value a caller that never speaks to a model wants —
            keeps the desk's deterministic rendering. The server bootstrap
            supplies :func:`creek_mcp.server._build_author_llm`; tests pass a
            stub, which is what makes the wire past the desk assertable.
        medium: Target medium (``research`` or ``chat``).
        max_rounds: Optional override for the voice/reflect round bound.
            Defaults to the vault's ``author.max_author_rounds`` config (3),
            mirroring the ``creek author --max-rounds`` help (#1465).
        dry_run: When set, return the plan + evidence summary, not a draft.
        privacy_tier_ceiling: Privacy gate (FEAT-010).
        consumer: Audit consumer identifier.

    Returns:
        On success, the draft envelope (body, verdict, provenance, cited
        ``claims``) or — for ``dry_run`` — the plan + evidence summary. A draft
        the desk's privacy check condemned yields ``status: refused`` instead,
        carrying the ``privacy_findings`` but none of the three content keys
        (#1353); see :func:`_draft_response`. An unsupported medium, or any
        desk failure, yields ``status: error`` with a ``reason``.
    """
    # #660: the ceiling is enforced — converted to a PrivacyTierOverride below
    # and threaded into the specialists, which exclude above-ceiling fragments
    # from the retrieved evidence.
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "query": query,
            "medium": medium,
            "dry_run": dry_run,
            "max_rounds": max_rounds,
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    try:
        require_supported_medium(medium)
    except ValueError as exc:
        return _error_response(
            str(exc), tier_ceiling=privacy_tier_ceiling, dry_run=dry_run
        )

    # Any desk failure (bad config, provider error, missing contract, a
    # downstream validation error, ...) must surface as a structured envelope,
    # never an unhandled exception across the MCP boundary. This mirrors the
    # broad boundary catch the other MCP write tools use (e.g. purge).
    # #660: enforce the ceiling by converting it to the core privacy override
    # threaded into the specialists.
    override = to_privacy_override(privacy_tier_ceiling)
    try:
        if dry_run:
            plan = plan_author(
                medium=medium,
                query=query,
                vault=vault_path,
                override=override,
            )
            return {
                "status": "ok",
                "tool": TOOL_NAME,
                "tier_ceiling": privacy_tier_ceiling.value,
                "tier_ceiling_enforced": True,
                "dry_run": True,
                "medium": medium,
                "query": query,
                "plan": plan["plan"],
                "evidence": plan["evidence"],
            }
        # #658/#661: the injected factory is tier-keyed so live voicing of
        # Intimate content is redirected to a local provider by the chokepoint,
        # and may answer ``None`` (deterministic stub) for an unavailable
        # provider — so the tool never hard-fails on a missing backend.
        draft = run_author(
            medium=medium,
            query=query,
            vault=vault_path,
            max_rounds=max_rounds,
            voice_client_factory=llm_factory,
            override=override,
        )
    except Exception as exc:
        return _error_response(
            str(exc), tier_ceiling=privacy_tier_ceiling, dry_run=dry_run
        )
    return _draft_response(draft, tier_ceiling=privacy_tier_ceiling)
