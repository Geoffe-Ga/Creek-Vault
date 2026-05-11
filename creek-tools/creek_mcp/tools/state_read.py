"""``creek.state.read`` MCP tool — cheap read of ``State/latest.md``.

This is the FEAT-010 cheap path: no re-render, no walking the vault,
just hand back the most recent audit-report bytes. ``creek.state.render``
in :mod:`creek_mcp.tools.state` regenerates the report; ``read`` exists
so CrawDad's Discord turn-around stays fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling

_STATE_LATEST_RELPATH = Path("00-Creek-Meta/State/latest.md")
TOOL_NAME = "creek.state.read"


def state_read_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Return the latest audit-report bytes plus tool metadata.

    The audit report aggregates eddy/thread/synchronicity *titles* —
    it never embeds fragment bodies — so the tier-ceiling enforcement
    here is the gate on whether the report itself can be returned at
    all, not a body-level filter. A missing report yields a structured
    "no report yet" status rather than a crash so a fresh vault stays
    usable.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"vault_path": str(vault_path)},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    report_path = vault_path / _STATE_LATEST_RELPATH
    if not report_path.exists():
        return {
            "status": "empty",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "report_path": str(_STATE_LATEST_RELPATH),
            "content": "",
        }
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_path": str(_STATE_LATEST_RELPATH),
        "content": report_path.read_text(encoding="utf-8"),
    }
