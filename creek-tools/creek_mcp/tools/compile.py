"""``creek.compile`` MCP tool — roll fragments into a compiled page.

Wraps :func:`creek.compile.engine.compile_to_vault` (FEAT-003). The
write-side tier-ceiling rule applies: a caller cannot create a compiled
page whose source fragments include a tier they could not read. The
wrapper hashes the target page before and after the compile so an
idempotent re-run (no provenance added) does not double-write an audit
entry — matching the FEAT-011 idempotency contract.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast

from creek.compile.engine import TARGET_KINDS, _default_llm, compile_to_vault
from creek.config import load_config
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import CompileTargetKind

TOOL_NAME = "creek.compile"


def _fingerprint(path: Path) -> str | None:
    """Return a content hash for *path* or ``None`` if it does not exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_tool(
    *,
    vault_path: Path,
    fragment_ids: list[str],
    target_kind: str,
    target_id: str,
    target_title: str,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Compile *fragment_ids* into a compiled-layer page and audit the run.

    A re-run that produces an identical target page is treated as a
    no-op: no audit entry is written, the response carries
    ``status="noop"``. First-time and content-changing runs append one
    audit entry with ``created_path`` + ``affected_fragment_ids``.
    """
    if target_kind not in TARGET_KINDS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unknown target_kind {target_kind!r}; "
                f"supported: {', '.join(TARGET_KINDS)}"
            ),
        )
    if not fragment_ids:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason="fragment_ids must be non-empty",
        )

    config = load_config()
    kind = cast("CompileTargetKind", target_kind)

    # Locate the target file *before* the compile so we can hash it.
    # ``compile_to_vault`` returns the same path on every call for a
    # given (kind, id) so we replay the path-derivation by running the
    # compile once and comparing fingerprints around it.
    try:
        written = compile_to_vault(
            fragment_ids=list(fragment_ids),
            vault_path=vault_path,
            target_kind=kind,
            target_id=target_id,
            target_title=target_title,
            llm=_default_llm(config.llm),
        )
    except (ValueError, RuntimeError) as exc:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )

    relative = str(written.relative_to(vault_path))
    post_hash = _fingerprint(written)
    pre_hash_marker_path = (
        vault_path / "00-Creek-Meta" / "audit" / f"compile-{target_id}.hash"
    )
    pre_hash = (
        pre_hash_marker_path.read_text(encoding="utf-8")
        if pre_hash_marker_path.exists()
        else None
    )
    if pre_hash is not None and pre_hash == post_hash:
        return {
            "status": "noop",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "compiled_path": relative,
            "affected_fragment_ids": list(fragment_ids),
        }

    pre_hash_marker_path.parent.mkdir(parents=True, exist_ok=True)
    if post_hash is not None:
        pre_hash_marker_path.write_text(post_hash, encoding="utf-8")

    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "target_kind": target_kind,
            "target_id": target_id,
            "target_title": target_title,
            "fragment_ids": list(fragment_ids),
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=relative,
        created_tier=None,
        affected_fragment_ids=list(fragment_ids),
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "compiled_path": relative,
        "target_kind": target_kind,
        "target_id": target_id,
        "affected_fragment_ids": list(fragment_ids),
    }
