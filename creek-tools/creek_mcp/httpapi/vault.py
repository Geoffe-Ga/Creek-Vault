"""Which vault a ``/v1`` request reads, resolved in one place (#1075).

Every content route needs the same answer to the same question, and the answer
has two halves that must not be split: the vault the app was *built* with wins,
and otherwise ``creek_config.yaml`` is read **per request** so an operator who
repairs a broken config gets a working server on the next call rather than after
a restart.

Two routes resolving that separately is how one of them ends up caching the
config while the other re-reads it, at which point "did my fix take effect?"
depends on which endpoint you ask. It lived inside
:mod:`creek_mcp.httpapi.capabilities` while the handshake was the only real
endpoint; #1075 gave it a second caller, so it moved here rather than being
copied.

:data:`UNREADABLE_CONFIG` is deliberately three concrete exception groups rather
than ``Exception``. A bug in the resolver must surface as a ``500`` from the
error boundary; only a genuinely unreadable configuration may degrade to "no
vault".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from yaml import YAMLError

from creek.classify.llm.consent import admits_request_keys
from creek.classify.llm.request_key import (
    REQUEST_KEY_PROVIDERS,
    RequestKey,
    parse_request_key,
)
from creek.config import load_config
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

UNREADABLE_CONFIG: Final = (OSError, ValueError, YAMLError)
""""The vault is not usable" when it is raised rather than returned.

:class:`FileNotFoundError` and a permission error are :class:`OSError`; a
malformed ``vault_path`` and every :class:`pydantic.ValidationError` are
:class:`ValueError`; an unparseable document is a :class:`yaml.YAMLError`.
"""


def configured_vault(request: Request) -> Path | None:
    """Return the vault *request* should read, resolving config if need be.

    **Blocking. Call it from a worker thread, never from a handler coroutine.**
    The fallback is a file read and a YAML parse, and it is not the rare arm:
    ``app.state.vault_path`` is ``None`` for the production entry point, since
    :func:`creek_mcp.httpapi.cli.main` builds the app without one. A caller that
    resolves the vault before entering its worker
    (:func:`creek_mcp.httpapi.deadline.read_off_loop` or
    :func:`~creek_mcp.httpapi.deadline.write_off_loop`) therefore does file I/O
    on the event loop for every request, stalling every other connection the
    process is serving and leaving
    :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
    unable to fire for that window — its cancel scope is evaluated on the loop.
    Every ``/v1`` route calls this from inside its own threadpool-wrapped
    worker, and ``tests/test_v1_api_hardening.py`` pins each one.

    Args:
        request: The request in flight.

    Returns:
        The explicitly configured vault path, the one the config names, or
        ``None`` when the configuration cannot be read at all.
    """
    configured: Path | None = request.app.state.vault_path
    return resolve_vault(configured)


def resolve_vault(configured: Path | None) -> Path | None:
    """Return *configured*, or the vault ``creek_config.yaml`` names, or ``None``.

    The one resolution rule, shared by :func:`configured_vault` — which applies
    it per ``/v1`` request — and by :func:`creek_mcp.httpapi.app.create_app`,
    which applies it **once**, at build, for the ``/mcp`` surface: MCP tools
    are bound to their vault when the server is built (#1409), exactly as
    ``creek-tools-mcp`` binds them.

    **Blocking**, for the reason :func:`configured_vault` gives.

    Args:
        configured: The explicitly configured vault, or ``None`` to resolve it
            from configuration.

    Returns:
        The vault path, or ``None`` when the configuration cannot be read.
    """
    if configured is not None:
        return configured
    try:
        resolved = load_config().vault_path
    except UNREADABLE_CONFIG:
        return None
    return resolved


def request_key_of(request: Request) -> RequestKey | None:
    """Return the request-scoped key *request* carries, or ``None``.

    Args:
        request: The request in flight.

    Returns:
        The parsed key, or ``None`` when neither header was sent.

    Raises:
        InvalidRequestKeyError: When the headers are present but malformed.
            The handler answers ``invalid_request``, whose body is one fixed
            message per code and echoes neither header.
    """
    return parse_request_key(
        request.headers.get(PROVIDER_HEADER),
        request.headers.get(PROVIDER_KEY_HEADER),
    )


def request_key_providers() -> list[str]:
    """Return the providers this vault will spend a writer's own key on.

    Reported on ``GET /v1/capabilities`` so a consumer can tell, before it
    forwards a key, whether the key would be used. Empty on a self-connected
    vault, which ignores request keys.

    Returns:
        The request-key providers on a managed vault, else an empty list.
    """
    return list(REQUEST_KEY_PROVIDERS) if admits_request_keys() else []
