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
from creek.config import load_vault_config
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
    retier: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Run the chosen classifier and return per-method counts.

    Classification rewrites the frontmatter of existing fragments. The
    tier-ceiling parameter is recorded for the audit trail; it does not
    gate execution because classify never creates new fragments — it
    only updates the labels on existing ones. It is also not the
    *widening* operation the name might suggest: the tier pass is
    escalate-only, so a run can move a fragment out of a remote
    consumer's reach and never into it.

    Args:
        vault_path: Vault root to classify.
        method: ``"rules"`` or ``"llm"``.
        force: Overwrite ``classification_method: manual`` decisions.
        retier: Re-derive the privacy tier of fragments that already
            carry a concrete one, persisting it only when the new
            verdict is *stricter* (issue #1106). Surfaced here because
            it is the only setting under which a caller who declared the
            wrong tier at write time can be corrected — a case
            ``POST /v1/uploads`` makes routine, since it requires an
            explicit tier and never re-derives it (#1497).
        privacy_tier_ceiling: Recorded for the audit trail.
        consumer: Who is calling, for the audit trail.

    Returns:
        A structured result: per-method counts on success, or a
        refusal for an unknown method or an unreachable provider.
    """
    if method not in _VALID_METHODS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unknown method {method!r}; supported: {', '.join(_VALID_METHODS)}"
            ),
        )
    # This config selects llm.provider — i.e. which model is shown the
    # vault's text — so it must be the classified vault's own file (#1409).
    config = load_vault_config(vault_path)
    try:
        summary = run_classify(
            vault_path=vault_path,
            config=config,
            method=method,
            force=force,
            retier=retier,
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
        args={"method": method, "force": force, "retier": retier},
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
        # Issue #1356: the LLM-failed skips used to roll into the field above,
        # so a run against a downed provider reported itself as a run in which
        # the rules were confident. Surfaced separately because only this one
        # tells the consumer the corpus is under-classified and the pass is
        # worth repeating.
        "llm_call_failed": summary.llm_call_failed,
        # Issue #876: how many fragments this run gave a real privacy
        # tier. Surfaced separately from ``classified`` because the tier
        # pass also runs on fragments the resume short-circuit preserved.
        "privacy_tiers_assigned": summary.privacy_tiers_assigned,
        # Issue #1106 / #1570: the subset of the above where an
        # already-recorded tier was replaced by a stricter one. Reported
        # separately because ``privacy_tiers_assigned`` alone cannot tell
        # "this fragment had no tier" from "this fragment had the wrong
        # one", and only the second is evidence that ``retier`` did work.
        "retiered": summary.retiered,
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
        # Issue #1357: how many fragments this run refused to treat as
        # already-LLM-classified because the pre-#1358 weighted soft-failure
        # path stamped them without any LLM running. Surfaced here for the
        # same reason as on the CLI: every other field on this payload counts
        # such a fragment as done.
        "healed_unearned_llm": summary.healed_unearned_llm,
        "errors": list(summary.errors),
    }
