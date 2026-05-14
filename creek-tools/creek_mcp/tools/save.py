"""``creek.save`` MCP tool — answer-filing-back primitive (FEAT-011).

Wraps :func:`creek.save.save_to_vault` (FEAT-009). The write-side
tier-ceiling rule is enforced here: a save that *would create* content
at tier ``T`` requires the caller's ``privacy_tier_ceiling`` to admit
``T``. Sending an ``intimate`` body with ``ceiling=open`` is refused
with a structured error rather than silently downgraded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.models import PrivacyTier
from creek.save import SaveRequest, SaveTarget, save_to_vault
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    write_tier_allowed,
)

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.save"


def save_tool(
    *,
    vault_path: Path,
    target: str,
    body: str,
    title: str | None = None,
    tier: str = "open",
    provenance: list[str] | None = None,
    source_kind: str = "mcp",
    source_id: str | None = None,
    saved_by: str = "mcp",
    full_body: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Save *body* to the vault and return the written path + provenance.

    The caller's ``privacy_tier_ceiling`` gates the requested ``tier``:
    a write that would create ``intimate`` content via ``ceiling=open``
    is refused. The full body never enters the audit log; only the
    written path, created tier, and the source-fragment ID list do.
    """
    try:
        save_target = SaveTarget(target)
    except ValueError:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"unknown target {target!r}",
        )
    try:
        save_tier = PrivacyTier(tier)
    except ValueError:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"unknown tier {tier!r}",
        )

    if not write_tier_allowed(save_tier, privacy_tier_ceiling):
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args={"target": target, "tier": tier, "body_len": len(body)},
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(f"tier {tier!r} exceeds ceiling {privacy_tier_ceiling.value!r}"),
        )

    request = SaveRequest(
        target=save_target,
        body=body,
        title=title,
        tier=save_tier,
        provenance=tuple(provenance or ()),
        source_kind=source_kind,
        source_id=source_id,
        saved_by=saved_by,
        full_body=full_body,
    )
    written = save_to_vault(request, vault_path=vault_path)
    relative = str(written.relative_to(vault_path))
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "target": target,
            "tier": tier,
            "title": title,
            "body_len": len(body),
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=relative,
        created_tier=save_tier.value,
        affected_fragment_ids=list(request.provenance),
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "saved_path": relative,
        "target": target,
        "created_tier": save_tier.value,
        "affected_fragment_ids": list(request.provenance),
    }
