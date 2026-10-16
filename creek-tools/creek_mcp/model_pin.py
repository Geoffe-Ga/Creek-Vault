"""The pinned-local-model rule for every MCP path that shows vault text to a model.

B05 (#1849) pinned ``creek.reflect`` to the operator's model package. Compile,
draft, the Writing Desk's voice client and ``creek.classify --method llm``
resolve their provider from the same writable vault config, so they share the
rule this module owns:

- **Outside both modes** (no :data:`~creek_mcp.model_package.MODEL_PACKAGE_FILE_ENV`
  and no :data:`~creek.classify.llm.local_boundary.LOOPBACK_ONLY_ENV`) nothing
  changes: a provider may serve when it reports itself available. A
  self-hoster's own cloud key keeps working.
- **With a manifest configured, or in container mode,** only the pinned local
  model may serve: an Ollama provider on a loopback URL whose model is the
  manifest's pinned tag, listed by the runtime at the pinned inventory digest.
  A cloud provider is refused whatever key or consent is present, and a
  configured manifest that cannot be loaded fails closed.

The check runs per request, because the vault config it reads is writable
after boot. It is one ``/api/tags`` call; the generation canary stays
probe-only.

**The one exception (ADR-0015, #1873).** On a managed vault
(:data:`~creek.classify.llm.consent.MANAGED_VAULT_ENV`), a request that carries
the *writer's own* Anthropic or OpenAI key may be served by that vendor, for
that request only: :func:`request_key_admitted` decides it and
:func:`build_request_scoped_provider` builds the provider. Nothing else moves.
The operator's configured cloud key is still refused, a self-connected vault
ignores a request key entirely, and the tier decision stays with the
:class:`~creek.classify.llm.router.ModelRouter`: callers resolve the request
key's stage through it, so INTIMATE is forced local or refused before this
module is asked anything.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from creek.classify.llm.consent import admits_request_keys
from creek.classify.llm.local_boundary import is_loopback_url, loopback_only_enforced
from creek.classify.llm.request_key import REQUEST_KEY_PROVIDERS
from creek_mcp.model_package import (
    MODEL_PACKAGE_FILE_ENV,
    ModelPackageError,
    configured_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek.classify.llm.base import LLMProvider
    from creek.classify.llm.request_key import RequestKey
    from creek.config import LLMConfig

PINNED_PROVIDER: Final[str] = "ollama"
"""The only provider a pin mode admits; checked on the config before any build."""


def model_pin_enforced(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether only the pinned local model may serve vault content.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        ``True`` when a model package is configured (loadable or not) or the
        process runs in loopback-only container mode.
    """
    source = os.environ if environ is None else environ
    configured = bool(source.get(MODEL_PACKAGE_FILE_ENV, "").strip())
    return configured or loopback_only_enforced(source)


def pin_refuses_stage(cfg: LLMConfig) -> bool:
    """Return whether a pin mode refuses *cfg* on its provider alone.

    Decided from the config, before any provider is built. A cloud provider's
    constructor raises a bare ``RuntimeError`` when its key or consent is
    missing, which is the normal state in a container, so building it first
    would surface the SDK's message instead of the caller's own refusal.

    Args:
        cfg: The resolved stage configuration.

    Returns:
        ``True`` when a pin mode applies and *cfg* is not an Ollama stage.
    """
    return model_pin_enforced() and cfg.provider != PINNED_PROVIDER


def provider_may_serve(provider: LLMProvider) -> bool:
    """Return whether *provider* may be shown vault content right now.

    Args:
        provider: The provider built from the request's resolved stage.

    Returns:
        ``provider.available`` outside both pin modes; otherwise
        :func:`pinned_model_serves`.
    """
    if not model_pin_enforced():
        return provider.available
    return pinned_model_serves(provider)


def pinned_model_serves(provider: LLMProvider) -> bool:
    """Return whether *provider* is the pinned model, served at its digest.

    Every refusal is decided before any dial except the last one, the
    inventory check, so a cloud provider or a remote URL is never contacted.

    Args:
        provider: The provider built from the request's resolved stage.

    Returns:
        ``True`` only for an Ollama provider on a loopback URL whose model is
        the manifest's pinned tag, listed at the pinned inventory digest.
    """
    from creek.classify.llm.providers import OllamaProvider, check_ollama_available

    try:
        manifest = configured_manifest()
    except ModelPackageError:
        return False
    if manifest is None or not isinstance(provider, OllamaProvider):
        return False
    cfg = provider.config
    if not is_loopback_url(cfg.ollama_url) or provider.model != manifest.model_name:
        return False
    return check_ollama_available(
        cfg,
        timeout=OllamaProvider.AVAILABILITY_TIMEOUT,
        expected_digest=manifest.runtime_inventory_digest,
    )


def request_key_admitted(
    cfg: LLMConfig,
    request_key: RequestKey | None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether *cfg* is a stage the writer's request key may serve.

    Admitted only when every one of these holds, so each refusal is the
    unchanged pre-ADR-0015 behaviour:

    - a request key was supplied;
    - this is a managed vault whose marker is affirmative (a self-connected
      vault, or a marker of ``0`` or ``""``, keeps its own
      operator-chosen routing and ignores a request key);
    - *cfg* names the key's own provider, and that provider is one a writer may
      bring (:data:`~creek.classify.llm.request_key.REQUEST_KEY_PROVIDERS`).

    The tier is deliberately absent. Callers pass the stage the
    :class:`~creek.classify.llm.router.ModelRouter` resolved for the request's
    tier, so an INTIMATE request reaches here already redirected to a local
    stage, which names no request-key provider and is never admitted.

    Args:
        cfg: The stage configuration the router resolved for this request.
        request_key: The writer's request-scoped key, or ``None``.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        ``True`` when the request key may build this stage's provider.
    """
    return (
        request_key is not None
        and admits_request_keys(environ)
        and cfg.provider == request_key.provider
        and cfg.provider in REQUEST_KEY_PROVIDERS
    )


def build_request_scoped_provider(request_key: RequestKey) -> LLMProvider:
    """Build the provider a writer's request-scoped key powers.

    The one place a provider is constructed with a request key. The key is
    handed to the provider in memory; the provider never reads the environment
    for a key or for consent while it holds one, so the operator's key can
    never stand in for the writer's.

    Args:
        request_key: The writer's key, already admitted by
            :func:`request_key_admitted`.

    Returns:
        An Anthropic or OpenAI provider bound to *request_key*, built from
        :meth:`~creek.classify.llm.request_key.RequestKey.stage_config`.
    """
    from creek.classify.llm.providers import AnthropicProvider, OpenAIProvider

    cfg = request_key.stage_config()
    if request_key.provider == "openai":
        return OpenAIProvider(cfg, request_key=request_key)
    return AnthropicProvider(cfg, request_key=request_key)
