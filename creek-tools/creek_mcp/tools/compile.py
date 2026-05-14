"""``creek.compile`` MCP tool — roll fragments into a compiled page.

Wraps :func:`creek.compile.engine.compile_to_vault` (FEAT-003). The
write-side tier-ceiling rule applies: a caller cannot create a compiled
page whose source fragments include a tier they could not read.

**Idempotency semantics (audit-dedup only).** The wrapper fingerprints
the target file after each compile and stamps the hash under
``00-Creek-Meta/audit/compile-<target_id>.hash``. On a re-run whose
output matches the stored hash, the wrapper returns ``status="noop"``
**and skips the audit-log append** — but the underlying engine still
ran (LLM call + file write). This satisfies the FEAT-011 acceptance
test ("re-invoking ``creek.compile`` doesn't double-write audit
entries on no-op runs"); it deliberately does *not* attempt to skip
the LLM call itself, because that would require an input fingerprint
(source fragment IDs + bodies + prompt) the wrapper does not own.
Engine-side LLM-call dedup is tracked separately as a follow-up.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Protocol, cast

from creek.compile.engine import TARGET_KINDS, compile_to_vault
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import CompileTargetKind

TOOL_NAME = "creek.compile"


class CompileLLMFactory(Protocol):
    """Zero-argument callable returning the prompt → JSON-text LLM client.

    The factory is invoked lazily so an unconfigured LLM provider only
    fails the ``creek.compile`` invocation, not server startup. The
    server bootstrap supplies a production factory; tests pass a stub.
    """

    def __call__(self) -> object: ...


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
    llm_factory: CompileLLMFactory,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Compile *fragment_ids* into a compiled-layer page and audit the run.

    A re-run whose output matches the last hash returns ``status="noop"``
    and the wrapper skips the audit-log append. The engine still runs
    every call; see the module docstring for the idempotency scope.

    Args:
        llm_factory: Zero-argument callable returning the compile LLM
            client. Invoked lazily so the LLM provider only matters
            when this tool is actually called.
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

    kind = cast("CompileTargetKind", target_kind)
    try:
        written = compile_to_vault(
            fragment_ids=list(fragment_ids),
            vault_path=vault_path,
            target_kind=kind,
            target_id=target_id,
            target_title=target_title,
            llm=llm_factory(),
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
