"""``creek.author`` MCP tool — typed stub Writing Desk response (FEAT-041, #456).

Mirrors the ``creek author`` CLI args over stdio and returns the shape an MCP
client should expect — a draft body plus provenance and a reflection verdict —
*before* the verb is wired to the real desk. The stub builds an
:class:`~creek.author.models.AuthoredDraft` locally; issue #460 replaces the
stub with a real ``run_author`` call. Existing MCP verbs are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from creek.author import AuthoredDraft, Medium
from creek.author.conductor import _require_supported_medium
from creek.compile.provenance import ProvenanceEntry
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.author"

_EXCERPT_LEN = 80


def _stub_draft(medium: Medium, query: str) -> AuthoredDraft:
    """Build a typed stub :class:`AuthoredDraft` for *query* (skeleton).

    Args:
        medium: The validated medium.
        query: The user query.

    Returns:
        A stub draft with one mock provenance entry and a ``PASS`` verdict.
    """
    provenance = [
        ProvenanceEntry(
            claim_id="claim-001",
            claim_excerpt=f"Stub claim for {query}"[:_EXCERPT_LEN],
            fragment_ids=["frag-stub-0001"],
            compiled_at=datetime.now(tz=UTC),
            compile_method="manual",
        )
    ]
    return AuthoredDraft(
        medium=medium,
        query=query,
        body=f"# {query} (MCP stub)\n\nStub response pending real desk wiring (#460).",
        provenance=provenance,
        verdict="PASS",
        rounds=1,
    )


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
    """Return a typed stub Writing Desk response for *query*.

    Mirrors the ``creek author`` CLI args. The response is a stub — issue #460
    wires it to the real desk — but the *shape* is final so MCP clients can
    integrate now.

    Args:
        vault_path: The vault the desk would author from.
        query: The user query to author about.
        medium: Target medium; only ``"research"`` is wired.
        max_rounds: Accepted for CLI parity; recorded, not yet applied.
        dry_run: Accepted for CLI parity; echoed back in the response.
        privacy_tier_ceiling: Privacy gate (FEAT-010).
        consumer: Audit consumer identifier.

    Returns:
        A dict with ``status``/``tool``/``tier_ceiling``. On success it also
        carries the stub ``medium``/``query``/``body``/``provenance``/
        ``verdict``/``rounds`` and the echoed ``dry_run``. An unsupported
        medium yields ``status: error`` with a ``reason``.
    """
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
        # Reuse the conductor's single source of truth for medium validation
        # rather than re-implementing the membership check and cast here.
        validated = _require_supported_medium(medium)
    except ValueError as exc:
        return {
            "status": "error",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "dry_run": dry_run,
            "reason": str(exc),
        }
    # The stub always reports ``rounds=1``; ``max_rounds`` is accepted for CLI
    # parity and recorded in the audit log, but the real round count arrives
    # when #460 wires the verb to the conductor.
    draft = _stub_draft(validated, query)
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "dry_run": dry_run,
        "medium": draft.medium,
        "query": draft.query,
        "body": draft.body,
        "provenance": [entry.model_dump(mode="json") for entry in draft.provenance],
        "verdict": draft.verdict,
        "rounds": draft.rounds,
    }
