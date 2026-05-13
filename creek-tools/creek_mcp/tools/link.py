"""``creek.link`` MCP tool — run a single linker stage (FEAT-011).

Wraps :func:`creek.link.link_engine.run_link`. Links land back in
fragment / thread / eddy frontmatter; the tool reports counts only.
``affected_fragment_ids`` stays empty because the linker does not
report per-ID changes back to the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.config import load_config
from creek.link.link_engine import run_link
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.link"
_VALID_METHODS = ("embeddings", "temporal", "eddies")


def link_tool(
    *,
    vault_path: Path,
    method: str = "embeddings",
    rebuild: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Run a single linker stage and return its counts.

    Linking updates existing artefacts in place. The tier-ceiling
    parameter is recorded for the audit trail; like ``classify`` the
    linker does not produce new tiered content, so the ceiling is not
    a gate here — every caller can re-link.
    """
    if method not in _VALID_METHODS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unknown method {method!r}; supported: {', '.join(_VALID_METHODS)}"
            ),
        )
    config = load_config()
    summary = run_link(
        vault_path=vault_path,
        config=config,
        method=method,
        rebuild=rebuild,
    )
    # Linking updates existing artefacts in place; no new file is
    # produced, so ``created_path`` is omitted per the audit-schema
    # convention documented in docs/mcp.md.
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"method": method, "rebuild": rebuild},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "method": summary.method,
        "fragment_count": summary.fragment_count,
        "link_count": summary.link_count,
    }
