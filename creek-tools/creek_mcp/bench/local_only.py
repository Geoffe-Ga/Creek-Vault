"""The guard that keeps every benchmark trial on a local model.

The harness exists to size a *local* model envelope, so a cloud provider
answering a trial would both corrupt the measurement and egress synthetic
prompts. :class:`LocalOnlyFactory` therefore refuses twice:

- at construction, a registered provider name that is not positively local
  (:func:`creek.classify.llm.providers.provider_intimate_eligible`). That
  covers every cloud vendor and, since #1894, the shared inference pool:
  the pool is not cloud, yet it egresses, so sizing a *local* envelope with
  it would both corrupt the measurement and send synthetic prompts off the
  machine. An *unregistered* name (the harness's own ``fake``) is still
  admitted and judged by the callable's own flag below;
- on every build, a callable that does not positively declare
  ``is_cloud = False``. A missing attribute counts as cloud — fail closed.

The runner accepts nothing but this type, and calls :meth:`preflight` before
it writes a corpus or sends a request, so a refused provider costs nothing.

**Self-declaration is not proof.** A callable's ``is_cloud`` flag says what
its author intended, not where its requests go. For a live run,
:func:`require_local_target` therefore derives locality from the request
target itself: the endpoint must resolve only to loopback or private
addresses (or the operator must pass ``--allow-remote-host``, which the report
records), and a model tag Ollama would forward to its cloud (``*-cloud``,
``*:cloud``) is refused outright.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from creek.classify.llm.providers import known_providers, provider_intimate_eligible
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek_mcp.bench.protocols import LLMCallable, LLMFactory

_CLOUD_REFUSED: Final[str] = "the benchmark refuses a cloud provider"
_PREFLIGHT_MAX_TOKENS: Final[int] = 1
"""Output ceiling for the preflight build; nothing is generated with it."""


_CLOUD_TAG: Final[re.Pattern[str]] = re.compile(r"[:-]cloud$")
"""Ollama's cloud-offload tag suffix: a local daemon forwards these off-host."""

_CLOUD_TAG_REFUSED: Final[str] = (
    "the benchmark refuses a cloud-offload model tag (*-cloud, *:cloud)"
)
_REMOTE_REFUSED: Final[str] = (
    "the benchmark refuses a non-local --ollama-url; pass --allow-remote-host "
    "to measure a remote runtime deliberately"
)
_NO_HOST: Final[str] = "the benchmark refuses an --ollama-url with no host"

EndpointScope = Literal["loopback", "private", "remote"]
"""Where a live run's requests go, judged from the resolved addresses."""


def _resolve(host: str) -> list[str]:
    """Return every address *host* resolves to; empty when it does not."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


def endpoint_scope(
    url: str, *, resolve: Callable[[str], list[str]] = _resolve
) -> EndpointScope:
    """Classify *url*'s host by the addresses it actually resolves to.

    Every address must be loopback for ``loopback`` and loopback-or-private
    for ``private``; one public address, or none at all, makes it ``remote``.

    Raises:
        CloudProviderRefusedError: When *url* names no host.
    """
    host = urlsplit(url).hostname
    if not host:
        raise CloudProviderRefusedError(_NO_HOST)
    addresses = [ipaddress.ip_address(address) for address in resolve(host)]
    if addresses and all(address.is_loopback for address in addresses):
        return "loopback"
    if addresses and all(a.is_private or a.is_loopback for a in addresses):
        return "private"
    return "remote"


def require_local_target(
    url: str,
    model_tag: str,
    *,
    allow_remote: bool,
    resolve: Callable[[str], list[str]] = _resolve,
) -> EndpointScope:
    """Refuse a live target that is not demonstrably local.

    Args:
        url: The Ollama endpoint.
        model_tag: The model the run will drive.
        allow_remote: The operator's explicit opt-in to a public endpoint.
        resolve: Host resolver (injectable for tests).

    Returns:
        The endpoint's scope, for the report.

    Raises:
        CloudProviderRefusedError: For a cloud-offload tag, a URL with no host,
            or a remote endpoint without *allow_remote*.
    """
    if _CLOUD_TAG.search(model_tag):
        raise CloudProviderRefusedError(_CLOUD_TAG_REFUSED)
    scope = endpoint_scope(url, resolve=resolve)
    if scope == "remote" and not allow_remote:
        raise CloudProviderRefusedError(_REMOTE_REFUSED)
    return scope


def _offloads(provider_name: str) -> bool:
    """Return whether *provider_name* is a registered, non-local backend.

    Args:
        provider_name: The provider's registry name.

    Returns:
        ``True`` for a registered provider that is not positively local — a
        cloud vendor or the shared inference pool. ``False`` for ``ollama``,
        the attested enclave, and any name the registry does not know (the
        harness's own ``fake``), whose callable is judged on its own flag.
    """
    return provider_name in known_providers() and not provider_intimate_eligible(
        provider_name
    )


class CloudProviderRefusedError(ValueError):
    """A cloud (or undeclared) provider was offered to the harness."""


class LocalOnlyFactory:
    """Wraps a model factory and refuses anything not positively local."""

    def __init__(self, inner: LLMFactory, *, provider_name: str) -> None:
        """Refuse a non-local *provider_name* before anything is built.

        Args:
            inner: The factory producing completion callables.
            provider_name: The provider's registry name (``ollama``, ``fake``).

        Raises:
            CloudProviderRefusedError: When *provider_name* is a registered
                provider that is not positively local — a cloud vendor, or the
                shared inference pool (see :func:`_offloads`).
        """
        if _offloads(provider_name):
            raise CloudProviderRefusedError(_CLOUD_REFUSED)
        self._inner = inner

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        """Build a callable and refuse it unless it declares itself local.

        Raises:
            CloudProviderRefusedError: When the callable is, or may be, cloud.
        """
        llm = self._inner(tier, max_tokens=max_tokens)
        if getattr(llm, "is_cloud", True):
            raise CloudProviderRefusedError(_CLOUD_REFUSED)
        return llm

    def preflight(self) -> None:
        """Build once without calling the model, so a refusal comes first.

        Raises:
            CloudProviderRefusedError: When the built callable is not local.
        """
        self(PrivacyTier.OPEN, max_tokens=_PREFLIGHT_MAX_TOKENS)
