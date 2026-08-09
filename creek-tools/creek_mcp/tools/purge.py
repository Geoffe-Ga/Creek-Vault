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

**Three statuses, not two** (#1246). ``refused`` means the gate said no
and nothing was touched. ``ok`` means the erasure is complete.
``partial`` means the operation ran to the end and something it
promised to erase is still on disk — read ``voice_body_undecodable``
for the fragment ids. The engine has always recorded that distinction
in ``purge.jsonl`` and the CLI has always printed it; this surface used
to report every non-refusal as ``ok``, which made it the one place an
incomplete erasure looked clean. See :data:`_ENGINE_STATUS_TO_WIRE`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

from creek.purge import PurgeEngine
from creek.purge.engine import VAULT_PURGE_CONFIRMATION, PurgeResult
from creek_mcp.audit import MCPAuditLog
from creek_mcp.auth import is_elevated
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

    from creek.purge.audit import PurgeOutcomeStatus

_FRAGMENT_TOOL = "creek.purge.fragment"
_SOURCE_TOOL = "creek.purge.source"
_CLASSIFICATIONS_TOOL = "creek.purge.classifications"
_DATERANGE_TOOL = "creek.purge.daterange"
_VAULT_TOOL = "creek.purge.vault"

_REFUSAL_NO_TOKEN = (
    "elevated authorization required: set CREEK_MCP_ELEVATED_TOKEN on the "
    "server and pass a matching auth_token"
)

_ENGINE_STATUS_TO_WIRE: Final[dict[PurgeOutcomeStatus, str]] = {
    "complete": "ok",
    "partial": "partial",
}
"""Wire spelling of :attr:`PurgeResult.outcome_status` (contract 0.4).

Three values now reach a caller of ``creek.purge.*``: ``refused`` (the
gate said no and nothing was touched), ``ok`` (the erasure is complete),
and ``partial`` (the operation finished, and something it promised to
erase is still on disk — see ``voice_body_undecodable``).

The mapping is not the identity because the two halves have different
back-compatibility obligations. ``complete`` keeps the ``ok`` spelling
every existing client already branches on; ``partial`` is new, and a
client that does not know it will fall through its ``ok``/``refused``
branches rather than mistake an incomplete erasure for a clean one.
Failing an unknown-status check is the safe direction here — silently
reading ``partial`` as success is the defect #1246 reports.

Adding the member moves the *tool surface's* semantics, so it carries a
``CONTRACT_VERSION`` minor bump (0.3.0 → 0.4.0) with 0.3 and 0.2 still
served (:data:`creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS`). No
``/v1`` HTTP shape changes: ``creek.purge.*`` is gated by the elevated
token, is out of the Adepthood surface per the contract ADR, and
appears in no route or published schema.
"""


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
    """Render a :class:`PurgeResult` as the structured tool response.

    Every field of the model is forwarded, because the payload's field
    set is *derived* from :class:`PurgeResult` rather than hand-picked.
    The hand-picked subset this replaces had silently dropped six fields
    by the time #1246 counted them — including
    ``voice_body_undecodable``, a non-empty list naming the fragments
    whose derived voice artifacts were **not** swept. A caller that
    cannot see that field cannot know the erasure was incomplete, and
    the field went missing precisely because a human had to remember to
    add it. Now nobody has to: add a field to ``PurgeResult`` and it
    reaches the caller.

    ``status`` and ``tool`` are the two keys that are *not* model
    fields — they describe the call, not the result — so they are
    written after the dump and win any future name collision. That is
    deliberate: a ``PurgeResult.status`` would be answering a different
    question (engine outcome, ``complete``/``partial``) than this one
    (call outcome, including refusals the engine never saw), and the
    wire meaning of ``status`` must not shift under an existing client.

    See :data:`_ENGINE_STATUS_TO_WIRE` for the two success spellings.
    """
    return {
        **result.model_dump(mode="json"),
        "status": _ENGINE_STATUS_TO_WIRE[result.outcome_status],
        "tool": tool,
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
