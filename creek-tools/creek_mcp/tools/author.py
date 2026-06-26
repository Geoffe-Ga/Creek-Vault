"""``creek.author`` MCP tool — author via the real Writing Desk (FEAT-041 #460).

A thin adapter: it validates the request, records the audit entry, and
delegates to ``creek.author.run_author`` (or ``plan_author`` for a dry run) —
no desk logic lives in the MCP layer (SPEC §4.3: both the CLI and MCP surfaces
call the same desk). The response carries the draft body, the reflection
verdict, per-claim provenance, and the cited ``claims`` (each with its
``source_fragments``). Existing MCP verbs are untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.author import plan_author, require_supported_medium, run_author
from creek.author.client import AuthorLLMClient
from creek.config import load_config
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author import AuthoredDraft

TOOL_NAME = "creek.author"


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


def _draft_response(
    draft: AuthoredDraft,
    *,
    tier_ceiling: TierCeiling,
) -> dict[str, Any]:
    """Render an :class:`AuthoredDraft` into the MCP success envelope."""
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
    }


def author_tool(
    *,
    vault_path: Path,
    query: str,
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
        medium: Target medium (``research`` or ``chat``).
        max_rounds: Optional override for the voice/reflect round bound.
        dry_run: When set, return the plan + evidence summary, not a draft.
        privacy_tier_ceiling: Privacy gate (FEAT-010).
        consumer: Audit consumer identifier.

    Returns:
        On success, the draft envelope (body, verdict, provenance, cited
        ``claims``) or — for ``dry_run`` — the plan + evidence summary. An
        unsupported medium yields ``status: error`` with a ``reason``.
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
        # #658: build the router-resolved voice client so the desk renders live
        # voicing; ``for_voice_or_none`` returns ``None`` (deterministic stub)
        # when the provider is unavailable, so the tool never hard-fails on a
        # missing/unconsented backend.
        config = load_config()
        voice_client = AuthorLLMClient.for_voice_or_none(
            config.model_router,
            author=config.author,
        )
        draft = run_author(
            medium=medium,
            query=query,
            vault=vault_path,
            max_rounds=max_rounds,
            llm_client=voice_client,
            override=override,
        )
    except Exception as exc:
        return _error_response(
            str(exc), tier_ceiling=privacy_tier_ceiling, dry_run=dry_run
        )
    return _draft_response(draft, tier_ceiling=privacy_tier_ceiling)
