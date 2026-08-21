"""``creek.ingest`` MCP tool — wrap a single ingestor stage (FEAT-011).

The ingestor's default tier is ``personal`` (matches the CLI), so
``ceiling=open`` calls are refused before any source data is read.
``affected_fragment_ids`` records what the run produced; the ingestor
errors flow back in the response without entering the audit body.

**This tool runs the shared pipeline** —
:func:`creek.ingest.pipeline.run_ingest` — exactly as ``creek ingest`` and
``creek.journal`` do (#1467). It used to re-implement the write loop inline
with a bare ``writer.write_fragment``, which cost it three things at once: a
repeat ingest of an *edited* in-vault ``.md`` minted a new id and orphaned its
predecessor; nothing it wrote carried ``source.origin_key``, so the RTBF purge
sweep — which resolves its targets from exactly that field — could not see it;
and it was structurally *mute*, computing no advisories to drop or deliver.

The stated reason for keeping it separate had gone stale. It read: ``run_ingest``
with a ``ledger_source`` plus a *directory* ``input_path`` arms
:func:`creek.ingest.pipeline.tomb_missing_units` for that ledger. #1329 had
already moved that gate onto the ingestor's registry key rather than onto
holding a ledger, and this tool needs no ``ledger_source`` at all. The
collision hazard #953 fixes is likewise unreachable here:
:func:`~creek_mcp.path_confinement.resolve_within_vault` refuses anything
outside the vault, so every path this tool ingests takes the in-vault arm of
``derive_source_key``.

What *was* real is narrower: ``source_type="markdown"`` plus a directory input
does arm the tomb sweep, and soft-deleting vault content is authority this
surface has never held. That is closed by passing ``may_tomb=False`` — a
restrictive conjunct that can only subtract authority — rather than by
declining to run the pipeline and keeping the three defects above.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creek._containment import EscapingSymlinkError
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import run_ingest
from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.path_confinement import resolve_within_vault
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    write_tier_allowed,
)

TOOL_NAME = "creek.ingest"
DEFAULT_INGEST_TIER = PrivacyTier.PERSONAL


def ingest_tool(
    *,
    vault_path: Path,
    source_type: str,
    input_path: str,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Run a single ingestor and persist its fragments under the vault.

    The default ingestor tier is :data:`DEFAULT_INGEST_TIER` (personal);
    callers with ``ceiling=open`` cannot run an ingest because the
    fragments would exceed the ceiling. The wrapper refuses *before*
    touching the source file so no side effects occur on rejection.

    Args:
        vault_path: Vault root used for path validation and audit logging.
        source_type: Registry key selecting the ingestor to run.
        input_path: Source to ingest; may be absolute or relative to the
            vault root. The path must resolve *inside* the vault to prevent
            the MCP surface from ingesting arbitrary disk content. Refusals
            are checked in order: tier ceiling, unknown ``source_type``,
            path confinement, then existence.
        privacy_tier_ceiling: Caller's tier ceiling; the personal-tier
            default is refused when the ceiling is lower.
        consumer: Identifier of the calling client (recorded in audit).

    Returns:
        A dict with ``status`` (``ok`` / ``refused``), the count and ids of
        written fragments, and any per-fragment errors.
    """
    if not write_tier_allowed(DEFAULT_INGEST_TIER, privacy_tier_ceiling):
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args={"source_type": source_type, "input_path": input_path},
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"ingest default tier {DEFAULT_INGEST_TIER.value!r} "
                f"exceeds ceiling {privacy_tier_ceiling.value!r}"
            ),
        )
    ingestor_cls = INGESTOR_REGISTRY.get(source_type)
    if ingestor_cls is None:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"unknown source_type {source_type!r}",
        )
    resolved = resolve_within_vault(vault_path, input_path)
    if resolved is None:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"input_path {input_path!r} resolves outside the vault root; "
                "the ingest tool only operates on vault-relative paths."
            ),
        )
    if not resolved.exists():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"input path not found: {input_path}",
        )

    try:
        result = run_ingest(
            ingestor_cls=ingestor_cls,
            source_type=source_type,
            input_path=resolved,
            vault_path=vault_path,
            privacy_tier=DEFAULT_INGEST_TIER,
            may_tomb=False,
        )
    except EscapingSymlinkError as exc:
        # ``resolve_within_vault`` above confines the path the caller NAMED;
        # it says nothing about a link *underneath* a legitimately in-vault
        # source. ``run_ingest`` drives the ingestor's own walk, so the
        # containment refusal still raises through it. Refusals on this
        # surface are structured responses, so it must not surface as a
        # transport crash (#1294). ``exc.path`` names the link as walked,
        # never its resolved target.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(f"source tree contains a symlink that escapes it: {exc.path}"),
        )
    written_ids = result.fragment_ids

    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"source_type": source_type, "input_path": input_path},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=str(Path("01-Fragments")),
        created_tier=DEFAULT_INGEST_TIER.value,
        affected_fragment_ids=written_ids,
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "written": len(written_ids),
        "errors": result.errors,
        "affected_fragment_ids": written_ids,
        "created_tier": DEFAULT_INGEST_TIER.value,
        # ``ceiling_safe_warnings``, never ``warnings``: the operator
        # rendering may name real vault fragments, and this caller's ceiling
        # has not admitted them (#1372). The choice is the producer's, made in
        # ``run_ingest``; this surface only picks the channel.
        "warnings": result.ceiling_safe_warnings,
    }
