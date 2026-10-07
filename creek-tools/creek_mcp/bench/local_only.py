"""The guard that keeps every benchmark trial on a local model.

The harness exists to size a *local* model envelope, so a cloud provider
answering a trial would both corrupt the measurement and egress synthetic
prompts. :class:`LocalOnlyFactory` therefore refuses twice:

- at construction, a provider name registered as cloud
  (:func:`creek.classify.llm.providers.provider_is_cloud`);
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

from creek.classify.llm.providers import provider_is_cloud
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


class CloudProviderRefusedError(ValueError):
    """A cloud (or undeclared) provider was offered to the harness."""


class LocalOnlyFactory:
    """Wraps a model factory and refuses anything not positively local."""

    def __init__(self, inner: LLMFactory, *, provider_name: str) -> None:
        """Refuse a cloud *provider_name* before anything is built.

        Args:
            inner: The factory producing completion callables.
            provider_name: The provider's registry name (``ollama``, ``fake``).

        Raises:
            CloudProviderRefusedError: When *provider_name* is a cloud provider.
        """
        if provider_is_cloud(provider_name):
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
