"""MCP purge tools gated by elevated authorization (FEAT-012).

Wraps :class:`creek.purge.engine.PurgeEngine` so the developer's Claude
Code (configured with ``CREEK_MCP_ELEVATED_TOKEN``) can drive
destructive operations through MCP while CrawDad's Discord bot — which
deliberately is not given the token — fails closed. Every call
appends an audit entry to ``mcp.jsonl`` regardless of whether the
purge proceeded — including engine exceptions, which are caught and
audited via :func:`_audit_engine_error`. A silent denial (or silent
failure) would defeat the whole point of an elevated gate.

The five tools mirror the CLI:

* ``creek.purge.fragment`` — delete one fragment by ID.
* ``creek.purge.source`` — delete every fragment from a source platform.
* ``creek.purge.classifications`` — wipe classification metadata.
* ``creek.purge.daterange`` — delete fragments inside a date window.
* ``creek.purge.vault`` — destroy all vault content (preserves folders).

``creek.purge.vault`` additionally requires the caller to echo the
absolute path of the target vault back via ``confirm_vault_path``,
mirroring the CLI's interactive prompt. Both the token and the
confirmation are required — neither alone is sufficient.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from creek.purge import PurgeEngine
from creek.purge.engine import VAULT_PURGE_CONFIRMATION, PurgeResult
from creek_mcp.audit import MCPAuditLog
from creek_mcp.auth import is_elevated
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

_FRAGMENT_TOOL = "creek.purge.fragment"
_SOURCE_TOOL = "creek.purge.source"
_CLASSIFICATIONS_TOOL = "creek.purge.classifications"
_DATERANGE_TOOL = "creek.purge.daterange"
_VAULT_TOOL = "creek.purge.vault"

_REFUSAL_NO_TOKEN = (
    "elevated authorization required: set CREEK_MCP_ELEVATED_TOKEN on the "
    "server and pass a matching auth_token"
)


def _refusal(
    *,
    tool: str,
    reason: str,
) -> dict[str, Any]:
    """Build the canonical refusal payload for purge tools.

    Mirrors :func:`creek_mcp.tier_ceiling.refusal_response` but does
    not carry a tier ceiling — purge tools have no body to filter, so
    ``tier_ceiling`` would be misleading on the refusal payload.
    """
    return {
        "status": "refused",
        "tool": tool,
        "reason": reason,
    }


def _audit_refusal(
    vault_path: Path,
    *,
    tool: str,
    args: dict[str, Any],
    consumer: str,
) -> None:
    """Record a refused purge attempt in the MCP audit log.

    Refusals are audited so the timeline shows every attempt — silent
    denials would let a hostile or buggy client probe the gate without
    leaving a trail. The token never enters ``args``: callers strip it
    before calling this helper.
    """
    MCPAuditLog(vault_path).append(
        tool=tool,
        args=args,
        tier_ceiling=TierCeiling.OPEN,
        consumer=consumer,
    )


def _audit_success(
    vault_path: Path,
    *,
    tool: str,
    args: dict[str, Any],
    consumer: str,
    result: PurgeResult,
) -> None:
    """Record a successful purge in the MCP audit log."""
    MCPAuditLog(vault_path).append(
        tool=tool,
        args=args,
        tier_ceiling=TierCeiling.OPEN,
        consumer=consumer,
        affected_fragment_ids=list(result.affected_fragment_ids),
    )


def _result_payload(
    *,
    tool: str,
    result: PurgeResult,
) -> dict[str, Any]:
    """Render a :class:`PurgeResult` as the structured tool response."""
    return {
        "status": "ok",
        "tool": tool,
        "operation": result.operation,
        "target": result.target,
        "criteria": dict(result.criteria),
        "affected_fragment_ids": list(result.affected_fragment_ids),
        "fragments_affected": result.fragments_affected,
        "deleted_files": list(result.deleted_files),
        "wikilinks_removed": result.wikilinks_removed,
        "threads_updated": result.threads_updated,
        "eddies_updated": result.eddies_updated,
        "classifications_reset": result.classifications_reset,
        "dry_run": result.dry_run,
    }


def _audit_engine_error(
    vault_path: Path,
    *,
    tool: str,
    args: dict[str, Any],
    consumer: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Audit an engine-side exception and return a structured refusal.

    The module docstring promises every call writes an audit entry; an
    uncaught raise from :class:`PurgeEngine` would break that promise.
    Catching ``Exception`` is deliberate — engine failures can be any
    subclass (``OSError``, ``ValueError``, ``RuntimeError``) and a
    silent leak would skip the audit.

    ``BaseException`` is left uncaught so ``KeyboardInterrupt`` and
    ``SystemExit`` still propagate as the operator expects.
    """
    _audit_refusal(vault_path, tool=tool, args=args, consumer=consumer)
    return _refusal(tool=tool, reason=f"purge engine failed: {exc}")


def _gate(
    *,
    tool: str,
    auth_token: str | None,
    vault_path: Path,
    args: dict[str, Any],
    consumer: str,
) -> dict[str, Any] | None:
    """Return a refusal payload when the gate fails, otherwise ``None``.

    Routes through :func:`creek_mcp.auth.is_elevated` and emits a
    refusal audit entry on denial. Callers proceed with the purge
    only when this helper returns ``None``.
    """
    if is_elevated(auth_token):
        return None
    _audit_refusal(vault_path, tool=tool, args=args, consumer=consumer)
    return _refusal(tool=tool, reason=_REFUSAL_NO_TOKEN)


def purge_fragment_tool(
    *,
    vault_path: Path,
    fragment_id: str,
    auth_token: str | None,
    dry_run: bool = False,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Delete a single fragment by ID after passing the elevated gate."""
    args = {"fragment_id": fragment_id, "dry_run": dry_run}
    refusal = _gate(
        tool=_FRAGMENT_TOOL,
        auth_token=auth_token,
        vault_path=vault_path,
        args=args,
        consumer=consumer,
    )
    if refusal is not None:
        return refusal
    engine = PurgeEngine(vault_path, dry_run=dry_run)
    try:
        result = engine.purge_fragment(fragment_id)
    except Exception as exc:
        return _audit_engine_error(
            vault_path,
            tool=_FRAGMENT_TOOL,
            args=args,
            consumer=consumer,
            exc=exc,
        )
    _audit_success(
        vault_path,
        tool=_FRAGMENT_TOOL,
        args=args,
        consumer=consumer,
        result=result,
    )
    return _result_payload(tool=_FRAGMENT_TOOL, result=result)


def purge_source_tool(
    *,
    vault_path: Path,
    source_type: str,
    auth_token: str | None,
    dry_run: bool = False,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Delete every fragment ingested from *source_type*."""
    args = {"source_type": source_type, "dry_run": dry_run}
    refusal = _gate(
        tool=_SOURCE_TOOL,
        auth_token=auth_token,
        vault_path=vault_path,
        args=args,
        consumer=consumer,
    )
    if refusal is not None:
        return refusal
    engine = PurgeEngine(vault_path, dry_run=dry_run)
    try:
        result = engine.purge_source(source_type)
    except Exception as exc:
        return _audit_engine_error(
            vault_path,
            tool=_SOURCE_TOOL,
            args=args,
            consumer=consumer,
            exc=exc,
        )
    _audit_success(
        vault_path,
        tool=_SOURCE_TOOL,
        args=args,
        consumer=consumer,
        result=result,
    )
    return _result_payload(tool=_SOURCE_TOOL, result=result)


def purge_classifications_tool(
    *,
    vault_path: Path,
    auth_token: str | None,
    dry_run: bool = False,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Reset classification fields on every fragment to unclassified."""
    args = {"dry_run": dry_run}
    refusal = _gate(
        tool=_CLASSIFICATIONS_TOOL,
        auth_token=auth_token,
        vault_path=vault_path,
        args=args,
        consumer=consumer,
    )
    if refusal is not None:
        return refusal
    engine = PurgeEngine(vault_path, dry_run=dry_run)
    try:
        result = engine.purge_classifications()
    except Exception as exc:
        return _audit_engine_error(
            vault_path,
            tool=_CLASSIFICATIONS_TOOL,
            args=args,
            consumer=consumer,
            exc=exc,
        )
    _audit_success(
        vault_path,
        tool=_CLASSIFICATIONS_TOOL,
        args=args,
        consumer=consumer,
        result=result,
    )
    return _result_payload(tool=_CLASSIFICATIONS_TOOL, result=result)


def _parse_iso_date(value: str, *, field: str) -> date:
    """Parse *value* as an ISO date or raise ``ValueError`` naming *field*."""
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        msg = f"{field} is not a valid ISO date: {value!r}"
        raise ValueError(msg) from exc


def purge_daterange_tool(
    *,
    vault_path: Path,
    start: str,
    end: str,
    auth_token: str | None,
    dry_run: bool = False,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Delete fragments whose ``created`` date falls within ``[start, end]``."""
    args = {"start": start, "end": end, "dry_run": dry_run}
    refusal = _gate(
        tool=_DATERANGE_TOOL,
        auth_token=auth_token,
        vault_path=vault_path,
        args=args,
        consumer=consumer,
    )
    if refusal is not None:
        return refusal
    try:
        start_date = _parse_iso_date(start, field="start")
        end_date = _parse_iso_date(end, field="end")
    except ValueError as exc:
        _audit_refusal(vault_path, tool=_DATERANGE_TOOL, args=args, consumer=consumer)
        return _refusal(tool=_DATERANGE_TOOL, reason=str(exc))
    engine = PurgeEngine(vault_path, dry_run=dry_run)
    try:
        result = engine.purge_daterange(start_date, end_date)
    except Exception as exc:
        return _audit_engine_error(
            vault_path,
            tool=_DATERANGE_TOOL,
            args=args,
            consumer=consumer,
            exc=exc,
        )
    _audit_success(
        vault_path,
        tool=_DATERANGE_TOOL,
        args=args,
        consumer=consumer,
        result=result,
    )
    return _result_payload(tool=_DATERANGE_TOOL, result=result)


def purge_vault_tool(
    *,
    vault_path: Path,
    confirm_vault_path: str | None,
    auth_token: str | None,
    dry_run: bool = False,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Destroy every fragment, thread, eddy, and related file.

    Requires both the elevated token *and* ``confirm_vault_path``
    matching the resolved target vault path. The audit entry records
    that the operator confirmed the path (boolean, not the path string,
    so the audit log does not echo absolute filesystem paths back into
    a tier-aware artefact).
    """
    args = {
        "confirm_vault_path": confirm_vault_path is not None,
        "dry_run": dry_run,
    }
    refusal = _gate(
        tool=_VAULT_TOOL,
        auth_token=auth_token,
        vault_path=vault_path,
        args=args,
        consumer=consumer,
    )
    if refusal is not None:
        return refusal
    if confirm_vault_path is None:
        _audit_refusal(vault_path, tool=_VAULT_TOOL, args=args, consumer=consumer)
        return _refusal(
            tool=_VAULT_TOOL,
            reason=(
                "creek.purge.vault requires confirm_vault_path matching the "
                "target vault's absolute path"
            ),
        )
    if not _paths_equivalent(vault_path, confirm_vault_path):
        _audit_refusal(vault_path, tool=_VAULT_TOOL, args=args, consumer=consumer)
        return _refusal(
            tool=_VAULT_TOOL,
            reason=(
                "confirm_vault_path does not match the target vault's resolved path"
            ),
        )
    engine = PurgeEngine(vault_path, dry_run=dry_run)
    try:
        result = engine.purge_vault(VAULT_PURGE_CONFIRMATION)
    except Exception as exc:
        return _audit_engine_error(
            vault_path,
            tool=_VAULT_TOOL,
            args=args,
            consumer=consumer,
            exc=exc,
        )
    _audit_success(
        vault_path,
        tool=_VAULT_TOOL,
        args=args,
        consumer=consumer,
        result=result,
    )
    return _result_payload(tool=_VAULT_TOOL, result=result)


def _paths_equivalent(vault_path: Path, candidate: str) -> bool:
    """Return ``True`` when *candidate* resolves to *vault_path*."""
    try:
        resolved_candidate = type(vault_path)(candidate).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return resolved_candidate == vault_path.resolve(strict=False)
