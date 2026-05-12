"""``creek.state.render`` MCP tool — re-render the audit report.

Mirrors ``creek state`` from the CLI. Re-rendering walks the vault, so
callers should prefer :func:`creek_mcp.tools.state_read.state_read_tool`
when they only need the most recent rendered output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.state import StateReportGenerator
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.state.render"


def state_render_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Re-render the audit report and return the written file path.

    The rendered file lives under ``00-Creek-Meta/State/<iso-week>.md``;
    ``latest.md`` is refreshed atomically by the underlying generator.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    written = StateReportGenerator(vault_path=vault_path).write()
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_path": str(written.relative_to(vault_path)),
        "content": written.read_text(encoding="utf-8"),
    }
