"""``creek.classify`` MCP tool — re-classify existing fragments (FEAT-011).

Wraps :func:`creek.classify.classify_engine.run_classify`. The work is
in-place: existing fragment frontmatter is updated; no new content tier
is *created*. The wrapper still records the operation under the audit
log, with ``affected_fragment_ids`` left empty because the engine does
not return them today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.classify.classify_engine import (
    LLMProviderUnavailableError,
    run_classify,
)
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
    try:
        summary = run_classify(
            vault_path=vault_path,
            config=config,
            method=method,
            force=force,
        )
    except LLMProviderUnavailableError as exc:
        # The engine refuses to iterate when the configured LLM
        # provider is unreachable. Translate to a structured refusal
        # so MCP clients see a stable shape instead of an unhandled
        # ``RuntimeError`` traceback.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )
    # Classify rewrites existing frontmatter in place; it does not
    # produce a new file, so ``created_path`` is omitted from the audit
    # entry (per the audit-schema convention documented in docs/mcp.md).
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"method": method, "force": force},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "total": summary.total,
        "classified": summary.classified,
        "preserved_manual": summary.preserved_manual,
        # Issue #321: prior-LLM-preserved fragments used to roll into
        # ``preserved_manual``, misattributing automated state to the
        # operator. Surface them as a separate field so downstream
        # consumers can present the two reasons distinctly.
        "preserved_llm": summary.preserved_llm,
        "skipped_high_confidence": summary.skipped_high_confidence,
        # Issue #876: how many fragments this run gave a real privacy
        # tier. Surfaced separately from ``classified`` because the tier
        # pass also runs on fragments the resume short-circuit preserved.
        "privacy_tiers_assigned": summary.privacy_tiers_assigned,
        # Issue #877: how many fragments this run raised to a stronger
        # praxis potential. Surfaced for the same reason as the tier count
        # — the field previously had no producer at all, and a silent
        # producer is how that bug survived.
        "praxis_marked": summary.praxis_marked,
        # Issue #878: how many fragments this run gained a hashtag ``tags``
        # entry on. Same reasoning again — the field had no producer at all
        # until #878, and an invisible producer is how that bug survived
        # 35,330 fragments and an empty Tag Garden.
        "tags_extracted": summary.tags_extracted,
        "errors": list(summary.errors),
    }
