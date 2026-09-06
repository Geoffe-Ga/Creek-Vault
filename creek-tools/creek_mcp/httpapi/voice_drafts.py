"""``/v1/voice-drafts/{external_id}`` — durable AI draft resource (#1727).

The three verbs share one identity and one privacy posture.  A missing,
corrupt, deleted, or above-ceiling object always becomes the same
``privacy_refused`` envelope; no route turns caller-owned ids into a corpus
enumeration oracle.  Upserts additionally admit the *existing* tier while the
storage lock is held, so an ``open`` caller cannot overwrite a ``personal``
draft by presenting a lower incoming tier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypeAlias

from pydantic import ValidationError

from creek._fslock import VaultLockTimeoutError
from creek.models import PrivacyTier
from creek.voice_drafts import (
    VoiceDraftAccessDeniedError,
    VoiceDraftDocument,
    VoiceDraftRecord,
    VoiceDraftStorageError,
    VoiceDraftWriteResult,
    delete_voice_draft,
    read_voice_draft,
    upsert_voice_draft,
)
from creek_mcp.api.models import (
    OK_STATUS,
    ErrorCode,
    JournalAction,
    VoiceDraftAttribution,
    VoiceDraftDeleteResponse,
    VoiceDraftReadResponse,
    VoiceDraftUpsertRequest,
    VoiceDraftUpsertResponse,
    WireTierCeiling,
)
from creek_mcp.audit import MCPAuditLog
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import read_off_loop, write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.journal import admissible_external_id
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tier_ceiling import tier_allowed, write_tier_allowed

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

_UPSERT_AUDIT_TOOL: Final[str] = "creek.voice-draft.upsert"
_READ_AUDIT_TOOL: Final[str] = "creek.voice-draft.read"
_DELETE_AUDIT_TOOL: Final[str] = "creek.voice-draft.delete"

_WriteOutcome: TypeAlias = VoiceDraftWriteResult | ErrorCode | None
_ReadOutcome: TypeAlias = VoiceDraftRecord | ErrorCode | None
_DeleteOutcome: TypeAlias = VoiceDraftRecord | ErrorCode | None


async def _parsed_body(request: Request) -> VoiceDraftUpsertRequest | None:
    """Return a validated upsert body, collapsing every parse failure."""
    try:
        return VoiceDraftUpsertRequest.model_validate(await request.json())
    except (ValidationError, ValueError, UnicodeDecodeError):
        return None


def _audit(
    vault: Path,
    context: RequestContext,
    *,
    tool: str,
    args: dict[str, object],
) -> None:
    """Record one body-free attempt before resolving the addressed object."""
    MCPAuditLog(vault).append(
        tool=tool,
        args=args,
        tier_ceiling=context.ceiling,
        consumer=context.consumer,
    )


def _existing_is_admitted(context: RequestContext, tier: PrivacyTier) -> bool:
    """Apply the canonical read ceiling to one persisted draft tier."""
    return tier_allowed(tier, context.ceiling)


def _upsert(
    request: Request,
    external_id: str,
    parsed: VoiceDraftUpsertRequest,
    context: RequestContext,
) -> _WriteOutcome:
    """Resolve, audit, admit, and atomically upsert one draft off-loop."""
    vault = configured_vault(request)
    if vault is None:
        return None
    _audit(
        vault,
        context,
        tool=_UPSERT_AUDIT_TOOL,
        args={
            "has_external_id": True,
            "has_title": parsed.title is not None,
            "body_len": len(parsed.content),
            "tier": parsed.tier.value,
        },
    )
    tier = PrivacyTier(parsed.tier.value)
    if not write_tier_allowed(tier, context.ceiling):
        return ErrorCode.PRIVACY_REFUSED
    try:
        return upsert_voice_draft(
            vault_path=vault,
            external_id=external_id,
            document=VoiceDraftDocument(
                content=parsed.content,
                title=parsed.title,
                tier=tier,
            ),
            existing_is_admitted=lambda existing: _existing_is_admitted(
                context, existing
            ),
        )
    except VoiceDraftAccessDeniedError:
        return ErrorCode.PRIVACY_REFUSED
    except VaultLockTimeoutError:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    except (OSError, VoiceDraftStorageError):
        return ErrorCode.INTERNAL_ERROR


def _read(
    request: Request,
    external_id: str,
    context: RequestContext,
) -> _ReadOutcome:
    """Resolve, audit, and recall one admitted draft off-loop."""
    vault = configured_vault(request)
    if vault is None:
        return None
    _audit(
        vault,
        context,
        tool=_READ_AUDIT_TOOL,
        args={"has_external_id": True},
    )
    try:
        record = read_voice_draft(vault_path=vault, external_id=external_id)
    except (OSError, VoiceDraftStorageError):
        return ErrorCode.INTERNAL_ERROR
    if record is None or not _existing_is_admitted(context, record.tier):
        return ErrorCode.PRIVACY_REFUSED
    if record.tier not in (PrivacyTier.OPEN, PrivacyTier.PERSONAL):
        return ErrorCode.INTERNAL_ERROR
    return record


def _delete(
    request: Request,
    external_id: str,
    context: RequestContext,
) -> _DeleteOutcome:
    """Resolve, audit, and retract one admitted draft off-loop."""
    vault = configured_vault(request)
    if vault is None:
        return None
    _audit(
        vault,
        context,
        tool=_DELETE_AUDIT_TOOL,
        args={"has_external_id": True},
    )
    try:
        deleted = delete_voice_draft(
            vault_path=vault,
            external_id=external_id,
            existing_is_admitted=lambda existing: _existing_is_admitted(
                context, existing
            ),
        )
    except VoiceDraftAccessDeniedError:
        return ErrorCode.PRIVACY_REFUSED
    except VaultLockTimeoutError:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    except (OSError, VoiceDraftStorageError):
        return ErrorCode.INTERNAL_ERROR
    return ErrorCode.PRIVACY_REFUSED if deleted is None else deleted


def _upsert_response(
    result: VoiceDraftWriteResult,
    context: RequestContext,
) -> Response:
    """Project a storage write onto the closed upsert response model."""
    payload = VoiceDraftUpsertResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(context.ceiling.value),
        external_id=result.record.external_id,
        fragment_id=result.record.fragment_id,
        action=JournalAction(result.action.value),
        tier=WireTierCeiling(result.record.tier.value),
        attribution=VoiceDraftAttribution(),
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _read_response(record: VoiceDraftRecord, context: RequestContext) -> Response:
    """Project one admitted stored record onto the closed read model."""
    payload = VoiceDraftReadResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(context.ceiling.value),
        external_id=record.external_id,
        fragment_id=record.fragment_id,
        title=record.title,
        content=record.content,
        tier=WireTierCeiling(record.tier.value),
        attribution=VoiceDraftAttribution(),
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


async def handle_voice_draft_upsert(request: Request) -> Response:
    """Create or update one external-id-owned, AI-attributed Voice Draft."""
    context = context_of(request.scope)
    external_id = str(request.path_params["external_id"])
    if not admissible_external_id(external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    parsed = await _parsed_body(request)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await write_off_loop(_upsert, request, external_id, parsed, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if isinstance(result, ErrorCode):
        return error_response(result, context)
    return _upsert_response(result, context)


async def handle_voice_draft_read(request: Request) -> Response:
    """Recall one admitted Voice Draft by its caller-owned external id."""
    context = context_of(request.scope)
    external_id = str(request.path_params["external_id"])
    if not admissible_external_id(external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await read_off_loop(_read, request, external_id, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if isinstance(result, ErrorCode):
        return error_response(result, context)
    return _read_response(result, context)


async def handle_voice_draft_delete(request: Request) -> Response:
    """Retract one admitted Voice Draft by its caller-owned external id."""
    context = context_of(request.scope)
    external_id = str(request.path_params["external_id"])
    if not admissible_external_id(external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await write_off_loop(_delete, request, external_id, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if isinstance(result, ErrorCode):
        return error_response(result, context)
    payload = VoiceDraftDeleteResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(context.ceiling.value),
        external_id=result.external_id,
        action="deleted",
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)
