"""Loopback boundary for the local Ollama runtime (#1849).

:class:`~creek.classify.llm.providers.OllamaProvider` reports ``is_cloud =
False`` unconditionally, and the router's intimate-forces-local rule trusts
that label. ``LLMConfig.ollama_url`` is free-form, though, so without a check
the provider labelled *local* could dial any host on the network.

In container mode the runtime sets :data:`LOOPBACK_ONLY_ENV`, and every Ollama
request URL is then built through :func:`ollama_endpoint`, which refuses any
endpoint that is not a literal loopback address. The refusal is a
:class:`httpx.TransportError` so every caller that already degrades on an
unreachable daemon degrades the same way here, instead of crashing. Outside
container mode the boundary is off and a self-hosted LAN Ollama keeps working.

``tests/test_ollama_endpoint_invariant.py`` keeps this module the only place
an ``ollama_url`` is turned into a request URL.
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
