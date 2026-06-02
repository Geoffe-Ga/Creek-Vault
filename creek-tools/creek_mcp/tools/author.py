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
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author import AuthoredDraft

TOOL_NAME = "creek.author"


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
        # Explicit so callers never mistake the echo for enforcement; flips to
        # True when tier-filtered retrieval lands with the real specialists (#463).
        "tier_ceiling_enforced": False,
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
    caller's privacy ceiling is recorded and echoed across the boundary (real
    tier-filtered retrieval arrives with the real specialists, #463).

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
    # Privacy gap (#463): the ceiling is recorded and echoed across the
    # boundary, but the current stub specialists do not yet tier-filter
    # retrieval. Real enforcement lands with the real Graph/Retrieval agents.
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
        return {
            "status": "error",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "dry_run": dry_run,
            "reason": str(exc),
        }

    if dry_run:
        plan = plan_author(medium=medium, query=query, vault=vault_path)
        return {
            "status": "ok",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "tier_ceiling_enforced": False,
            "dry_run": True,
            "medium": medium,
            "query": query,
            "plan": plan["plan"],
            "evidence": plan["evidence"],
        }

    draft = run_author(
        medium=medium, query=query, vault=vault_path, max_rounds=max_rounds
    )
    return _draft_response(draft, tier_ceiling=privacy_tier_ceiling)
