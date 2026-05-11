"""``creek.lint`` MCP tool — unified vault hygiene checks (FEAT-008)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.lint import LintRunner, parse_since
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.lint"


def lint_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    checks: list[str] | None = None,
    since: str | None = None,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Run the unified lint pass and persist a markdown report.

    Lint reads only compiled-layer metadata (eddies, threads, tag
    indexes, paradoxes) so the tier ceiling does not gate any fragment
    bodies — but the parameter is still required per FEAT-010 to keep
    tool signatures uniform.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "vault_path": str(vault_path),
            "checks": list(checks) if checks else None,
            "since": since,
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    since_dt = parse_since(since) if since else None
    runner = LintRunner(vault_path=vault_path, since=since_dt, since_text=since)
    report = runner.run(checks=list(checks) if checks else None)
    written = runner.write(report)
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_path": str(written.relative_to(vault_path)),
        "checks": [
            {
                "name": result.name,
                "summary": result.summary,
                "finding_count": len(result.findings),
            }
            for result in report.results
        ],
    }
