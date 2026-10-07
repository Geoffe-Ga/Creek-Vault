"""Loopback boundary for the local Ollama runtime (#1849).

:class:`~creek.classify.llm.providers.OllamaProvider` reports ``is_cloud =
False`` unconditionally, and the router's intimate-forces-local rule trusts
that label. ``LLMConfig.ollama_url`` is free-form, though, so without a check
the provider labelled *local* could dial any host on the network.

Every Ollama request goes through :func:`ollama_get` / :func:`ollama_post`,
which own both halves of the dial:

- **The URL.** :func:`ollama_endpoint` builds it and, when the container
  runtime sets :data:`LOOPBACK_ONLY_ENV`, refuses any endpoint that is not a
  literal loopback address. The refusal is a :class:`httpx.TransportError`, so
  every caller that already degrades on an unreachable daemon degrades the same
  way here instead of crashing. Without the flag a self-hosted LAN Ollama keeps
  working.
- **The transport.** It always ignores the proxy environment, so a loopback
  URL cannot be carried to an ``HTTP_PROXY`` or ``ALL_PROXY`` host.

``tests/test_ollama_endpoint_invariant.py`` keeps this module the only place
an ``ollama_url`` is turned into a request URL, and every client it builds
proxy-free.
"""

from __future__ import annotations

import os
from ipaddress import ip_address
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping
    from urllib.parse import SplitResult

    from creek.config import LLMConfig

LOOPBACK_ONLY_ENV: Final[str] = "CREEK_OLLAMA_LOOPBACK_ONLY"
"""Process flag that confines every Ollama dial to a loopback endpoint."""

_DISABLED_VALUES: Final[frozenset[str]] = frozenset({"", "0"})
_LOOPBACK_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_LOOPBACK_NAME: Final[str] = "localhost"
_REFUSAL: Final[str] = (
    "Ollama endpoint is not a loopback address; "
    "refusing to dial it in loopback-only mode"
)


class NonLoopbackOllamaError(httpx.TransportError):
    """A refused dial to a non-loopback Ollama endpoint.

    The message is a fixed string: it never carries the configured URL, so a
    log line or a structured refusal cannot reveal where the config pointed.
    """

    def __init__(self) -> None:
        """Build the fixed, URL-free refusal."""
        super().__init__(_REFUSAL)


def is_loopback_url(url: str) -> bool:
    """Return whether *url* names a literal loopback HTTP(S) endpoint.

    Only ``localhost`` or an IP literal in the loopback range qualifies. No
    DNS lookup is made, so a name that merely *resolves* to loopback is
    refused, as are userinfo-bearing URLs (``http://127.0.0.1@host``), the
    unspecified address ``0.0.0.0`` and every private-network address.

    Args:
        url: The configured Ollama base URL.

    Returns:
        ``True`` only for a well-formed loopback HTTP(S) URL.
    """
    parts = _split(url)
    if parts is None or parts.scheme not in _LOOPBACK_SCHEMES:
        return False
    if parts.hostname is None or "@" in parts.netloc:
        return False
    return _is_loopback_host(parts.hostname)


def _split(url: str) -> SplitResult | None:
    """Return *url*'s parts, or ``None`` when its host or port is malformed."""
    try:
        parts = urlsplit(url)
        _ = (parts.hostname, parts.port)
    except ValueError:
        return None
    return parts


def _is_loopback_host(hostname: str) -> bool:
    """Return whether *hostname* is ``localhost`` or a loopback IP literal."""
    if hostname == _LOOPBACK_NAME:
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def loopback_only_enforced(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this process confines Ollama to loopback.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        ``True`` when :data:`LOOPBACK_ONLY_ENV` is set to anything other than
        an empty string or ``0``.
    """
    source = os.environ if environ is None else environ
    return source.get(LOOPBACK_ONLY_ENV, "").strip() not in _DISABLED_VALUES


def ollama_endpoint(config: LLMConfig, path: str) -> str:
    """Return the request URL for *path* on the configured Ollama runtime.

    Args:
        config: The stage's LLM configuration carrying ``ollama_url``.
        path: The API path, starting with ``/``.

    Returns:
        ``ollama_url`` joined with *path*.

    Raises:
        NonLoopbackOllamaError: In loopback-only mode, when ``ollama_url`` is
            not a loopback endpoint.
    """
    if loopback_only_enforced() and not is_loopback_url(config.ollama_url):
        raise NonLoopbackOllamaError
    return f"{config.ollama_url}{path}"


def _ollama_client(timeout: float) -> httpx.Client:
    """Return a client that never routes through an environment proxy.

    httpx's default ``trust_env=True`` sends even a loopback URL through
    ``HTTP_PROXY`` / ``ALL_PROXY`` unless ``NO_PROXY`` exempts it, which would
    carry a prompt to the proxy host while the provider stays labelled local.
    Ollama is local by contract, so this holds whether or not loopback-only
    mode is on.
    """
    return httpx.Client(timeout=timeout, trust_env=False)


def ollama_get(config: LLMConfig, path: str, *, timeout: float) -> httpx.Response:
    """``GET`` *path* on the configured Ollama runtime, bypassing env proxies.

    Args:
        config: The stage's LLM configuration carrying ``ollama_url``.
        path: The API path, starting with ``/``.
        timeout: HTTP timeout in seconds.

    Returns:
        The fully read response.

    Raises:
        NonLoopbackOllamaError: As :func:`ollama_endpoint`, before any dial.
        httpx.HTTPError: On a transport failure.
    """
    url = ollama_endpoint(config, path)
    with _ollama_client(timeout) as client:
        return client.get(url)


def ollama_post(
    config: LLMConfig,
    path: str,
    *,
    payload: dict[str, object],
    timeout: float,
) -> httpx.Response:
    """``POST`` JSON *payload* to *path* on the Ollama runtime, bypassing proxies.

    Args:
        config: The stage's LLM configuration carrying ``ollama_url``.
        path: The API path, starting with ``/``.
        payload: The JSON request body.
        timeout: HTTP timeout in seconds.

    Returns:
        The fully read response.

    Raises:
        NonLoopbackOllamaError: As :func:`ollama_endpoint`, before any dial.
        httpx.HTTPError: On a transport failure.
    """
    url = ollama_endpoint(config, path)
    with _ollama_client(timeout) as client:
        return client.post(url, json=payload)
