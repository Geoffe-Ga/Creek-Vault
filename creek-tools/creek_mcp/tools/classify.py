"""``creek.classify`` MCP tool — re-classify existing fragments (FEAT-011).

Wraps :func:`creek.classify.classify_engine.run_classify`. The work is
in-place: existing fragment frontmatter is updated; no new content tier
is *created*. The wrapper still records the operation under the audit
log, with ``affected_fragment_ids`` left empty because the engine does
not return them today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.classify.classify_engine import run_classify
from creek.config import load_config
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.classify"
_VALID_METHODS = ("rules", "llm")


def classify_tool(
    *,
    vault_path: Path,
    method: str = "rules",
    force: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Run the chosen classifier and return per-method counts.

    Classification rewrites the frontmatter of existing fragments. The
    tier-ceiling parameter is recorded for the audit trail; it does not
    gate execution because classify never creates new fragments — it
    only updates the labels on existing ones.
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
    summary = run_classify(
        vault_path=vault_path,
        config=config,
        method=method,
        force=force,
    )
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"method": method, "force": force},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path="01-Fragments",
        created_tier=None,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "total": summary.total,
        "classified": summary.classified,
        "preserved_manual": summary.preserved_manual,
        "skipped_high_confidence": summary.skipped_high_confidence,
        "errors": list(summary.errors),
    }
