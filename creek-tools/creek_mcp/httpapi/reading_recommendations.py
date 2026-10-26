"""Versioned reading recommendations through the vault's configured generation stack."""

from __future__ import annotations

import hashlib
import os
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from creek.classify.llm.providers import ProviderRateLimitError
from creek.config import resolve_config_path
from creek.models import PrivacyTier
from creek_mcp.api.models import ErrorCode
from creek_mcp.api.reading_recommendations import (
    ReadingRecommendation,
    ReadingRecommendationRequest,
    ReadingRecommendationResponse,
)
from creek_mcp.audit import MCPAuditLog
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.generation import generate_off_loop
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from creek_mcp.provenance import InferenceProvenance
from creek_mcp.reading_recommendations import (
    RecommendationPromptError,
    RecommendationUnavailableError,
    recommend_reading,
)
from creek_mcp.tier_ceiling import tier_allowed
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.tools.letter import LetterLLMFactory


def _factory(vault: Path, provenance: InferenceProvenance) -> LetterLLMFactory:
    """Reuse letters' generation router, model pin, pool caps and consent gate."""
    from creek_mcp.server import _build_letter_llm_factory

    return _build_letter_llm_factory(vault, None, provenance)


def _model_revision(vault: Path) -> str:
    """Invalidate results when the configured model or instructions change."""
    digest = hashlib.sha256()
    path = resolve_config_path(vault=vault, explicit=None)
    if path is not None:
        digest.update(path.read_bytes())
    manifest = os.environ.get(MODEL_PACKAGE_FILE_ENV, "").strip()
    if manifest:
        digest.update(Path(manifest).read_bytes())
    return digest.hexdigest()


def _generate(
    request: Request,
    parsed: ReadingRecommendationRequest,
    context: RequestContext,
    provenance: InferenceProvenance,
) -> tuple[bool, ReadingRecommendation | None]:
    """Keep filesystem reads, audit writes, caching and inference off-loop."""
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return False, None
    MCPAuditLog(vault).append(
        tool="creek.readings.recommend",
        args={
            "reading_count": len(parsed.readings),
            "source_count": len(parsed.sources),
        },
        tier_ceiling=context.ceiling,
        consumer=context.consumer,
    )
    result = recommend_reading(
        vault=vault,
        request=parsed,
        ceiling=context.ceiling,
        build_factory=request.app.state.letter_llm_factory
        or partial(_factory, vault, provenance),
        model_version=_model_revision(vault),
        provenance=provenance,
    )
    return True, result


async def handle_reading_recommendations(request: Request) -> Response:
    """Return one validated recommendation; never borrow the writer's BYOK key."""
    context = context_of(request.scope)
    try:
        parsed = ReadingRecommendationRequest.model_validate(await request.json())
    except (ValidationError, ValueError, UnicodeDecodeError):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    if any(
        not tier_allowed(PrivacyTier(source.tier), context.ceiling)
        for source in parsed.sources
    ):
        return error_response(ErrorCode.PRIVACY_REFUSED, context)
    provenance = InferenceProvenance()
    try:
        available, result = await generate_off_loop(
            context, _generate, request, parsed, context, provenance
        )
    except ProviderRateLimitError:
        return error_response(ErrorCode.RATE_LIMITED, context)
    except RecommendationPromptError:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    except (RecommendationUnavailableError, OSError):
        return error_response(ErrorCode.TEMPORARILY_UNAVAILABLE, context)
    if not available:
        return error_response(ErrorCode.UNAVAILABLE, context)
    payload = ReadingRecommendationResponse(
        status="ok" if result is not None else "empty",
        recommendation=result,
        inference_source=provenance.source if result is not None else None,
    )
    return json_response(payload.model_dump(mode="json", exclude_none=True), HTTP_OK)
