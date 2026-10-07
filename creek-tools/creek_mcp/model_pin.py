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
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from creek.classify.llm.local_boundary import is_loopback_url, loopback_only_enforced
from creek_mcp.model_package import (
    MODEL_PACKAGE_FILE_ENV,
    ModelPackageError,
    configured_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek.classify.llm.base import LLMProvider


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
