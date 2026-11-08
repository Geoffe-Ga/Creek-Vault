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

import logging
from typing import TYPE_CHECKING, Any

from creek._fslock import VaultLockTimeoutError
from creek.config import load_vault_config
from creek.link.embeddings import (
    EMBEDDING_MODEL_UNAVAILABLE_REASON,
    EmbeddingModelUnavailableError,
)
from creek.link.link_engine import run_link
from creek.surface_modes import LINK_METHODS
from creek.vault.mutations import (
    CONTENT_MUTATION_BUSY_REASON,
    ContentMutationBusyError,
)
from creek_mcp.audit import MCPAuditLog
from creek_mcp.grounding_route import link_embedder
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.link"

logger = logging.getLogger(__name__)


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

    Two refusals come after the vault is read and before anything is written
    or audited: a timed-out content lock answers
    :data:`~creek.vault.mutations.CONTENT_MUTATION_BUSY_REASON`, and an
    embedding model that cannot load — or a non-local route that refused a
    text (:class:`~creek.link.embeddings.EmbeddingRefusedError`) — answers
    :data:`~creek.link.embeddings.EMBEDDING_MODEL_UNAVAILABLE_REASON` on the
    ``embeddings``, ``eddies`` and ``threads`` stages (#2065). Each stage
    embeds before it writes, so neither leaves a partial pass behind.
    """
    if method not in LINK_METHODS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(f"unknown method {method!r}; supported: {', '.join(LINK_METHODS)}"),
        )
    # The vault being linked owns its own embeddings/linking knobs (#1409).
    config = load_vault_config(vault_path)
    try:
        summary = run_link(
            vault_path=vault_path,
            config=config,
            method=method,
            rebuild=rebuild,
            # A managed vault with no writer's key embeds through the attested
            # pool (#1904, ADR-0018); everywhere else this is ``None`` and the
            # local sentence-transformer is unchanged.
            embedder=link_embedder(config.model_router),
        )
    except (ContentMutationBusyError, VaultLockTimeoutError):
        # Acquiring the content lock past its wait is ContentMutationBusyError
        # (#2087). The bare-timeout arm is kept on purpose: a lock timeout
        # under the engine's hold still reads busy, as it always has, rather
        # than escaping as a FastMCP error or a /v1 job `internal_error`.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=CONTENT_MUTATION_BUSY_REASON,
        )
    except EmbeddingModelUnavailableError as exc:
        # The engine's message names the model, the cache directory and the
        # loader's own error; FastMCP would serve ``str(exc)`` to the client
        # as an ``isError`` result (#2065). The type name goes in the message
        # and the detail rides only the traceback, into the operator log.
        logger.exception(
            "link stage %s refused as unavailable: %s", method, type(exc).__name__
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=EMBEDDING_MODEL_UNAVAILABLE_REASON,
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
