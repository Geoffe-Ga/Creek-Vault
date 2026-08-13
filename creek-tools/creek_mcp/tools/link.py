"""``creek.link`` MCP tool — run a single linker stage (FEAT-011).

Wraps :func:`creek.link.link_engine.run_link`. Links land back in
fragment / thread / eddy frontmatter; the tool reports counts only.
``affected_fragment_ids`` stays empty because the linker does not
report per-ID changes back to the caller.

The accepted methods come from :data:`creek.surface_modes.LINK_METHODS`, the
same declaration ``creek link`` reads. Until #1252 this module carried a
retyped copy that had lost ``"threads"``, which put the entire thread half of
#880 — the fix for one thread swallowing 94% of a corpus — out of an MCP
caller's reach, with *"unknown method 'threads'"* as the only symptom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.config import load_config
from creek.link.link_engine import run_link
from creek.surface_modes import LINK_METHODS
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.link"


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

    The response carries the same cluster-health counts ``creek link``
    prints — ``largest_cluster_fragments``, ``clusters_split`` and
    ``oversized_discarded`` — because a discarded fragment is data loss and
    a caller who cannot see it reads a lossy pass as a clean one (#1372).
    """
    if method not in LINK_METHODS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(f"unknown method {method!r}; supported: {', '.join(LINK_METHODS)}"),
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
        # The cluster-health counts ``creek link`` renders on the console
        # (``creek.cli._format_cluster_stats``) and this tool used to drop
        # (#1372). Named rather than cited by line, because a line range in a
        # 5000-line module is wrong the next time anything above it moves. A
        # discard is data loss — those fragments carry no wiki-link at all —
        # so a caller who cannot see it believes the pass succeeded. All three
        # are plain ints on a frozen dataclass of counts
        # (creek/link/link_engine.py:63+): they can never name a fragment, so
        # unlike the ingest advisories they need no ceiling gate.
        "largest_cluster_fragments": summary.largest_cluster_fragments,
        "clusters_split": summary.clusters_split,
        "oversized_discarded": summary.oversized_discarded,
    }
