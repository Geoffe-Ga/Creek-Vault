"""Which embedding route grounds a managed vault's reflections (#1904, ADR-0018).

Release decision R4-4 wires the attested pool's embedding model for writers who
do **not** bring their own key; ADR-0018 records it as an amendment of
ADR-0004 for managed vaults only. This module is the one place that decision
is made, and it is made from configuration alone — no dial — because it runs
before grounding, and grounding runs before the generation provider is built.

The rule, in order:

1. **Not a managed vault** — the local sentence-transformer, exactly as
   ADR-0004 keeps it. Self-hosted vaults are untouched.
2. **A writer's key that will answer this request** — the local route,
   unchanged. With a writer's key the vendor answers and the pool is never
   dialled (ADR-0016 R9, T10); grounding is no exception.
3. **Generation for this tier does not resolve to the pool** — the local
   route. An INTIMATE reflection is redirected local by the router, and its
   grounding stays where its answer is made. This is also what keeps the
   published ``inference_source`` truthful (T15): pool embeddings are only
   ever spent on a request whose answer the pool itself gives, so the one
   label names the one destination every byte went to.
4. **Otherwise the attested pool** — when the Machine pins a pool destination
   and the operator's manifest pins an attested embedding model. When either
   is missing, the vendor offered no attested embedding model or the pin
   cannot be read: :class:`GroundingUnavailableError`, and the reflection is
   ungrounded (ADR-0016 Q2 option (iii)) with nothing sent anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from creek.classify.llm.consent import is_managed_vault
from creek.classify.llm.pool_boundary import pool_url_configured
from creek.classify.llm.pool_embeddings import PoolEmbedder, PoolEmbeddingPin
from creek.classify.llm.router import NON_INTIMATE_ROUTE, IntimateRoutingError
from creek_mcp.model_package import ModelPackageError, configured_manifest
from creek_mcp.model_pin import request_key_admitted

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek.classify.llm.request_key import RequestKey
    from creek.classify.llm.router import ModelRouter
    from creek.models import PrivacyTier

POOL_PROVIDER: Final[str] = "pool"
"""The ``LLMConfig.provider`` string of the shared inference pool."""

_NO_POOL: Final[str] = "no pool destination is pinned; grounding withheld"
_NO_PIN: Final[str] = "no attested embedding model is pinned; grounding withheld"
_BAD_MANIFEST: Final[str] = "the model package cannot be loaded; grounding withheld"


class GroundingUnavailableError(RuntimeError):
    """The pool was chosen for grounding but its embedding pin cannot be met.

    Every message is fixed and content-free. Raising — rather than returning
    the local route — is the point: ADR-0016 Q2's fallback is option (iii),
    ungrounded, so a pool-routed request whose pin fails is not quietly
    grounded some other way.
    """


def pinned_embedding(environ: Mapping[str, str] | None = None) -> PoolEmbeddingPin:
    """Return the attested embedding pin this Machine's manifest declares.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The pin.

    Raises:
        GroundingUnavailableError: When no pool destination is pinned, the
            manifest cannot be loaded, or it pins no embedding model.
    """
    if not pool_url_configured(environ):
        raise GroundingUnavailableError(_NO_POOL)
    try:
        manifest = configured_manifest(environ)
    except ModelPackageError as exc:
        raise GroundingUnavailableError(_BAD_MANIFEST) from exc
    if manifest is None or manifest.embedding is None:
        raise GroundingUnavailableError(_NO_PIN)
    pin = manifest.embedding
    return PoolEmbeddingPin(
        model_name=pin.model_name,
        dimensions=pin.dimensions,
        similarity_threshold=pin.similarity_threshold,
        runtime_inventory_digest=pin.runtime_inventory_digest,
    )


def _generation_on_pool(router: ModelRouter, tier: PrivacyTier) -> bool:
    """Return whether *tier*'s generation stage resolves to the pool.

    Configuration only: :meth:`ModelRouter.resolve` builds no provider and
    dials nothing. A tier the router refuses outright has no pool answer.
    """
    try:
        return router.resolve("generation", tier).provider == POOL_PROVIDER
    except IntimateRoutingError:
        return False


def grounding_embedder(
    router: ModelRouter,
    tier: PrivacyTier,
    request_key: RequestKey | None = None,
    environ: Mapping[str, str] | None = None,
) -> PoolEmbedder | None:
    """Return the embedder that grounds a *tier* request, ``None`` for local.

    Args:
        router: The served vault's model router.
        tier: The request's routing tier.
        request_key: The writer's request-scoped key, or ``None``.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The attested pool's embedder, or ``None`` to keep the local route.

    Raises:
        GroundingUnavailableError: When the pool is the route but its pin
            cannot be met; the caller grounds nothing.
    """
    if not is_managed_vault(environ):
        return None
    if request_key is not None and request_key_admitted(
        request_key.stage_config(), request_key, environ
    ):
        return None
    if not _generation_on_pool(router, tier):
        return None
    return PoolEmbedder(pinned_embedding(environ))


def link_embedder(
    router: ModelRouter, environ: Mapping[str, str] | None = None
) -> PoolEmbedder | None:
    """Return the embedder for a managed vault's bulk pass, ``None`` for local.

    The bulk pass behind ``POST /v1/links`` carries no writer's key — the
    route admits none — so it follows the keyless rule, asked for the route
    every non-INTIMATE tier shares. Each fragment's own tier is still handed
    to the chokepoint inside the embedder; this only picks the route. A pin
    that cannot be met keeps the local route, which on a managed image has no
    model and so embeds nothing — the pass sends no text anywhere.

    Args:
        router: The vault's model router.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The attested pool's embedder, or ``None``.
    """
    try:
        return grounding_embedder(router, NON_INTIMATE_ROUTE, None, environ)
    except GroundingUnavailableError:
        return None
