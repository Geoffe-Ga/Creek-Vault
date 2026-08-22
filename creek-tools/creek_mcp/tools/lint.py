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

    "Lint reads only compiled-layer metadata" is not true: the paradox
    check loads ``Fragment.model_validate(post.metadata)`` straight out of
    ``01-Fragments`` and ``10-Liminal`` (``creek/lint/checks/paradox.py``),
    and the unnamed / orphan-compiled checks also scan ``01-Fragments``.
    What actually keeps this tool ceiling-agnostic is narrower and holds up
    better: those checks read fragment *frontmatter*, never bodies, and
    this tool's response returns only ``name``, a count-shaped ``summary``,
    and ``len(findings)`` — verified against every ``CheckResult``
    construction under ``creek/lint/checks/``. The title- and path-bearing
    strings live in ``findings``, which this tool never returns. The
    written markdown report *does* embed titles and paths, but it is a
    vault-internal artifact no MCP tool reads back. The ceiling parameter
    is still required per FEAT-010 to keep tool signatures uniform.
    """
    resolved_checks = list(checks) if checks is not None else None
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "checks": resolved_checks,
            "since": since,
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    since_dt = parse_since(since) if since else None
    runner = LintRunner(vault_path=vault_path, since=since_dt, since_text=since)
    report = runner.run(checks=resolved_checks)
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
