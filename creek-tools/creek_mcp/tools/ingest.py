"""``creek.ingest`` MCP tool — wrap a single ingestor stage (FEAT-011).

The ingestor's default tier is ``personal`` (matches the CLI), so
``ceiling=open`` calls are refused before any source data is read.
``affected_fragment_ids`` records what the run produced; the ingestor
errors flow back in the response without entering the audit body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creek.ingest import INGESTOR_REGISTRY, assemble_ingested_fragment
from creek.models import PrivacyTier
from creek.vault.writer import VaultWriter
from creek_mcp.audit import MCPAuditLog
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
    source = Path(input_path)
    if not source.exists():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"input path not found: {input_path}",
        )

    writer = VaultWriter(vault_path=vault_path)
    ingest_result = ingestor_cls().ingest(source)
    written_ids: list[str] = []
    errors: list[str] = list(ingest_result.errors)
    for parsed in ingest_result.fragments:
        try:
            assembled = assemble_ingested_fragment(parsed)
            writer.write_fragment(assembled.fragment, body=assembled.body)
        except (KeyError, ValueError, OSError) as exc:
            errors.append(f"[{source_type}] {exc}")
            continue
        written_ids.append(assembled.fragment.id)

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
        "errors": errors,
        "affected_fragment_ids": written_ids,
        "created_tier": DEFAULT_INGEST_TIER.value,
    }
