"""``creek.mine`` MCP tool — surface essay seeds from the vault."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.mining import IdeaMiner
from creek.models import Phase
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.mine"
_DEFAULT_LIMIT = 10


def mine_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    phase: str = "unclassified",
    limit: int = _DEFAULT_LIMIT,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Mine essay seeds and return a structured, score-ranked list.

    The ceiling maps onto the existing
    :class:`creek.classify.privacy_filter.PrivacyTierOverride` so
    intimate fragments are excluded at the source — the MCP wrapper
    cannot widen access beyond what the CLI flag would permit. A
    ``limit`` of zero or negative returns every seed.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "vault_path": str(vault_path),
            "phase": phase,
            "limit": limit,
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    override = to_privacy_override(privacy_tier_ceiling)
    seeds = IdeaMiner(privacy_override=override).mine_all(
        vault_path,
        current_phase=Phase(phase),
    )
    display = seeds if limit <= 0 else seeds[:limit]
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "total": len(seeds),
        "seeds": [
            {
                "strategy": seed.strategy.value,
                "title": seed.title,
                "score": round(seed.score, 4),
                "source_fragments": list(seed.source_fragments),
            }
            for seed in display
        ],
    }
