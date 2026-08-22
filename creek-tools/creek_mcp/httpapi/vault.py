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

from creek.config import load_config

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
    resolves the vault before entering
    :func:`~starlette.concurrency.run_in_threadpool` therefore does file I/O on
    the event loop for every request, stalling every other connection the
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
    if configured is not None:
        return configured
    try:
        resolved = load_config().vault_path
    except UNREADABLE_CONFIG:
        return None
    return resolved
