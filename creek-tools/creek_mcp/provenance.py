"""Which route answered one model-backed ``/v1`` request (#1894, ADR-0016 D1.11).

ADR-0016 Decision 1 item 11 requires that every model-backed ``/v1`` answer
names its route. This module is the one place that decides what that name is,
and the one place a route reads it back.

**Why a recorder rather than a return value.** The tools that call a model take
an injected ``llm_factory`` and return prose; the thing that *knows* the route
is :func:`creek_mcp.server._generation_provider`, several frames below the
handler and inside a worker thread. Widening every tool's return shape to carry
a transport fact would put routing detail into the MCP surface as well as the
HTTP one. So the handler mints an :class:`InferenceProvenance`, hands it down
the factory chain it already threads a request key through, and reads it after
the off-loop call returns.

**It records the route, not the outcome.** The source is written when the
provider is resolved, which is the moment the destination is decided. A call
that then fails produces a refusal, and a refusal carries no
``inference_source`` at all — so there is no window in which this records a
destination that was never dialled.

**An unlabelled answer is an honest answer.** Three of the four published
values name a route this contract can stand behind; the fourth case — an
operator's own cloud key on a self-hosted vault — is none of them, so nothing
is recorded and the field is omitted. The consumer's use for the field is to
notice a route it did not expect, and a guessed label would defeat exactly
that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.classify.llm.pool_boundary import pool_attesting_vendor
from creek.classify.llm.providers import provider_is_cloud
from creek_mcp.api.models import (
    INFERENCE_SOURCE_LOCAL,
    INFERENCE_SOURCE_POOL_CREEK,
    attested_pool_source,
    request_key_source,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek.classify.llm.request_key import RequestKey

POOL_PROVIDER: str = "pool"
"""The ``LLMConfig.provider`` string the shared inference pool registers as."""


@dataclass
class InferenceProvenance:
    """The route one request's model answer came from, once it is known.

    Mutable and request-scoped: one of these lives for exactly as long as the
    handler that minted it, is written once by the factory that resolves the
    generation stage, and is read once by the renderer. It holds no prompt, no
    response and no destination — only a closed-vocabulary label.

    Attributes:
        source: The published provenance value, or ``None`` when no model was
            called or the route is one this contract does not label.
    """

    source: str | None = None

    def record(self, source: str | None) -> None:
        """Record *source* as this request's route.

        Args:
            source: A published provenance value, or ``None`` to leave the
                answer unlabelled. Recording ``None`` over an earlier value is
                deliberately possible: a factory invoked twice in one request
                must not leave the first call's label standing for the second.
        """
        self.source = source


def stage_inference_source(
    provider: str, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return the provenance value for a generation stage, or ``None``.

    Args:
        provider: The resolved stage's ``LLMConfig.provider``.
        environ: Environment to read the attesting vendor from; ``os.environ``
            when omitted.

    Returns:
        ``pool:creek`` or ``pool:attested:<vendor>`` for the shared pool,
        ``local`` for a provider that runs inside the vault's own trust
        boundary, and ``None`` for an operator's own cloud key — a route none
        of ADR-0016's four values names.
    """
    if provider == POOL_PROVIDER:
        vendor = pool_attesting_vendor(environ)
        return (
            INFERENCE_SOURCE_POOL_CREEK
            if vendor is None
            else attested_pool_source(vendor)
        )
    return None if provider_is_cloud(provider) else INFERENCE_SOURCE_LOCAL


def request_key_inference_source(request_key: RequestKey) -> str | None:
    """Return the provenance value for an answer bought with a writer's key.

    Args:
        request_key: The admitted request-scoped key that answered.

    Returns:
        ``request_key:<vendor>``, or ``None`` when the key's provider is not a
        name this vocabulary can carry.
    """
    return request_key_source(request_key.provider)
