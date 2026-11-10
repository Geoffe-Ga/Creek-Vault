"""``creek.mission`` MCP tool — a personal mission and values document for Reconcile.

The transport-neutral wrapper around :mod:`creek.generate.mission`. It admits
the corpus under the caller's ceiling, derives the routing tier, builds the
model client through the injected tier-keyed factory, and answers with the
saved note's path and the paste-ready document.

**The gates, in order.**

1. *Ceiling.* :func:`creek_mcp.tier_ceiling.to_privacy_override` converts the
   ceiling, and :func:`creek.generate.mission.select_mission_sources` applies
   it as a hard cutoff: an above-ceiling fragment contributes nothing to the
   prompt, not even a title. That is the whole reason the response may carry
   the document: every byte the model saw was admitted, so nothing it wrote
   can be above the ceiling the caller declared.
2. *Nothing to draw on.* With no admitted fragment of the owner's own, the
   tool answers ``empty`` and builds no provider: a mission from no notes
   would be invented.
3. *Routing.* The tier is :func:`creek_mcp.tier_ceiling.routing_tier` of the
   ceiling and the sources' own most sensitive tier, so a caller can neither
   declare a low ceiling to win cloud routing for personal sources nor pick
   sources to win it under a broad ceiling. The factory resolves the
   ``generation`` stage through the vault's router, so an ``INTIMATE`` call is
   forced local or refused. This module never picks a provider.

**Disclosure.** The derived routing tier never leaves this module: it is in no
response, refusal or audit row. The audit row carries the limit and the
caller's own ceiling only. A provider failure is answered with a fixed reason;
the vendor's text rides only the operator log's traceback, as on
``creek.draft`` (#2073).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, Protocol

from creek.classify.llm.providers import (
    PROVIDER_CALL_FAILURES,
    REQUEST_KEY_REJECTED_REASON,
    RequestKeyRejectedError,
)
from creek.classify.llm.router import IntimateRoutingError
from creek.config import CONFIG_LOAD_FAILURES, load_vault_config
from creek.generate.ai_style.fingerprint import load_fingerprint
from creek.generate.ai_style.preamble import build_style_preamble
from creek.generate.drafts import INFERENCE_SOURCE_FRONTMATTER_KEY
from creek.generate.mission import (
    DEFAULT_MISSION_LIMIT,
    MAX_MISSION_LIMIT,
    MISSION_MAX_OUTPUT_TOKENS,
    MissionParseError,
    content_tier,
    generate_mission,
    select_mission_sources,
)
from creek_mcp.audit import MCPAuditLog
from creek_mcp.config_refusal import config_unavailable_refusal
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    routing_tier,
    to_privacy_override,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.generate.mission import MissionResult, MissionSource
    from creek.models import PrivacyTier
    from creek_mcp.provenance import InferenceProvenance

__all__ = [
    "DEFAULT_MISSION_LIMIT",
    "MISSION_PROVIDER_FAILED_REASON",
    "MISSION_PROVIDER_UNAVAILABLE_REASON",
    "MISSION_UNREADABLE_REASON",
    "TOOL_NAME",
    "MissionLLMFactory",
    "mission_tool",
]

TOOL_NAME: Final[str] = "creek.mission"

logger = logging.getLogger(__name__)

MISSION_PROVIDER_UNAVAILABLE_REASON: Final[str] = "LLM provider unavailable for mission"
"""The fixed refusal when the mission provider could not be built.

Any failure of the tier-keyed factory other than the router's
:class:`~creek.classify.llm.router.IntimateRoutingError` or a refused request
key: a provider that is unavailable or not the pinned model, a constructor
that refused, or a vault config the factory could not load.
"""

MISSION_PROVIDER_FAILED_REASON: Final[str] = "LLM provider call failed for mission"
"""The fixed refusal when the provider was built but the call failed."""

MISSION_UNREADABLE_REASON: Final[str] = (
    "the model's answer could not be read as a mission; nothing was saved"
)
"""The fixed refusal when the answer held no mission with a value."""

_CONFIG_UNAVAILABLE_PREFIX: Final[str] = "mission unavailable: "
"""How a refusal for an unloadable vault config opens; a type name follows."""

_FACTORY_FAILURES: Final[tuple[type[Exception], ...]] = (
    *PROVIDER_CALL_FAILURES,
    *CONFIG_LOAD_FAILURES,
)
"""What building the client can raise: a provider fault, or the config it reads.

Named rather than starred inline because mypy rejects a starred ``except``
tuple.
"""


class _MissionLLM(Protocol):
    """A prompt-completion callable returning the model's text."""

    def __call__(self, prompt: str, /) -> str:
        """Return the completion for *prompt*."""


class MissionLLMFactory(Protocol):
    """Tier-keyed builder for the mission LLM client.

    The production factory resolves the ``generation`` stage through the
    vault's :class:`~creek.classify.llm.router.ModelRouter`; see
    :func:`creek_mcp.server._mission_factory_for`. Invoked once, lazily, and
    only when there are sources to draw on.
    """

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> _MissionLLM:
        """Return a routed completion callable with a hard output ceiling."""


def _refusal(ceiling: TierCeiling, reason: str) -> dict[str, Any]:
    """Return the structured refusal carrying *reason*."""
    return refusal_response(tool=TOOL_NAME, ceiling=ceiling, reason=reason)


def _provider_refusal(
    exc: Exception, ceiling: TierCeiling, *, default: str
) -> dict[str, Any]:
    """Log a provider failure for the operator and return its fixed refusal.

    Args:
        exc: What the factory or the model call raised.
        ceiling: The caller's declared ceiling, echoed by the refusal.
        default: The fixed reason for the branch that caught it.

    Returns:
        The refusal: the router's own message for an
        :class:`~creek.classify.llm.router.IntimateRoutingError` (the one-bit
        residual ``creek.draft`` documents and accepts),
        :data:`~creek.classify.llm.providers.REQUEST_KEY_REJECTED_REASON` for a
        writer's key its vendor refused, and *default* for anything else.
    """
    logger.exception(
        "mission refused on a provider failure: %s", type(exc).__name__, exc_info=exc
    )
    if isinstance(exc, IntimateRoutingError):
        return _refusal(ceiling, str(exc))
    if isinstance(exc, RequestKeyRejectedError):
        return _refusal(ceiling, REQUEST_KEY_REJECTED_REASON)
    return _refusal(ceiling, default)


def _style_preamble(vault_path: Path) -> str:
    """Return the owner's ``## Voice targets`` preamble.

    Reads the vault's own config, so it raises one of
    :data:`~creek.config.CONFIG_LOAD_FAILURES` when that cannot load; the
    caller refuses by type name, before any provider is built.
    """
    ai_style = load_vault_config(vault_path).ai_style
    return build_style_preamble(load_fingerprint(vault_path, ai_style), ai_style)


def _ok_response(
    vault_path: Path,
    ceiling: TierCeiling,
    result: MissionResult,
    provenance: InferenceProvenance | None,
) -> dict[str, Any]:
    """Return the success envelope for *result*."""
    response: dict[str, Any] = {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "mission_path": str(result.path.relative_to(vault_path)),
        "title": result.document.title,
        "markdown": result.markdown,
        "source_fragments": [source.fragment_id for source in result.sources],
        "source_count": len(result.sources),
        "value_count": len(result.document.values),
    }
    if provenance is not None and provenance.source is not None:
        response[INFERENCE_SOURCE_FRONTMATTER_KEY] = provenance.source
    return response


def _write(
    vault_path: Path,
    *,
    llm_factory: MissionLLMFactory,
    sources: list[MissionSource],
    ceiling: TierCeiling,
    style_preamble: str,
    provenance: InferenceProvenance | None,
) -> dict[str, Any]:
    """Build the routed client, prompt once, and save; or refuse."""
    tier = routing_tier(ceiling, content_tier(sources))
    try:
        llm = llm_factory(tier, max_tokens=MISSION_MAX_OUTPUT_TOKENS)
    except _FACTORY_FAILURES as exc:
        return _provider_refusal(
            exc, ceiling, default=MISSION_PROVIDER_UNAVAILABLE_REASON
        )
    source = None if provenance is None else provenance.source
    try:
        result = generate_mission(
            vault_path,
            llm=llm,
            sources=sources,
            style_preamble=style_preamble,
            inference_source=source,
        )
    except MissionParseError:
        return _refusal(ceiling, MISSION_UNREADABLE_REASON)
    except PROVIDER_CALL_FAILURES as exc:
        return _provider_refusal(exc, ceiling, default=MISSION_PROVIDER_FAILED_REASON)
    return _ok_response(vault_path, ceiling, result, provenance)


def mission_tool(
    *,
    vault_path: Path,
    llm_factory: MissionLLMFactory,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    limit: int = DEFAULT_MISSION_LIMIT,
    consumer: str = "unknown",
    provenance: InferenceProvenance | None = None,
) -> dict[str, Any]:
    """Draft, save and return the owner's mission and values for Reconcile.

    Args:
        vault_path: Vault root.
        llm_factory: Tier-keyed builder for the model client; invoked once,
            and only when there are sources to draw on.
        privacy_tier_ceiling: The caller's ceiling. Fragments above it are
            left out entirely, and it contributes to the routing tier.
        limit: How many source fragments to draw on, 1 to
            :data:`~creek.generate.mission.MAX_MISSION_LIMIT`.
        consumer: The authenticated consumer, for the audit row.
        provenance: The call's route recorder, filled by the production
            factory when it resolves the provider. Its label is stamped into
            the saved note and echoed as ``inference_source``.

    Returns:
        ``ok`` with ``mission_path``, ``title``, ``markdown`` (the body to
        paste into Reconcile), ``source_fragments``, ``source_count`` and
        ``value_count``; ``empty`` when no admitted fragment of the owner's
        own exists; or a structured ``refused`` response for a limit out of
        range, a vault config that cannot load, an unavailable provider, a
        failed call, or an answer that could not be read. No refusal names a
        fragment, its tier, or the provider's own error text.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"limit": limit},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    if not 1 <= limit <= MAX_MISSION_LIMIT:
        return _refusal(
            privacy_tier_ceiling, f"limit must be between 1 and {MAX_MISSION_LIMIT}"
        )
    sources = select_mission_sources(
        vault_path, override=to_privacy_override(privacy_tier_ceiling), limit=limit
    )
    if not sources:
        return {
            "status": "empty",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "reason": "no fragments of the owner's own within the ceiling",
        }
    try:
        style_preamble = _style_preamble(vault_path)
    except CONFIG_LOAD_FAILURES as exc:
        return config_unavailable_refusal(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            prefix=_CONFIG_UNAVAILABLE_PREFIX,
            exc=exc,
        )
    return _write(
        vault_path,
        llm_factory=llm_factory,
        sources=sources,
        ceiling=privacy_tier_ceiling,
        style_preamble=style_preamble,
        provenance=provenance,
    )
