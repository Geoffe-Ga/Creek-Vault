"""Where a vault serves MCP over streamable-HTTP: ``/mcp`` (#1962).

One constant and one predicate, framework-free, because three boundaries that
must agree about the path import them and none of them may import a web
framework to do it:

* the vault's own app (:mod:`creek_mcp.httpapi.mcp_mount`), which serves the
  **exact** path and nothing beneath it;
* the Fly edge (:mod:`creek_mcp.provisioning.pilot_control`), which selects the
  *vault realm* for the exact path **and** its subtree, so that what exists
  under ``/mcp`` is the vault realm's decision rather than the edge's;
* the replay router (:mod:`creek_mcp.httpapi.routing`), which replays the exact
  path to the owning vault.

It also names where a vault publishes its RFC 9728 protected-resource metadata
(#1963), for the same three readers: the vault serves the document, the edge
must select a realm for it before any credential exists, and the router answers
it there without one. The resource identifier is always the vault URL plus
:data:`MCP_PATH`, so the document's path is fixed too: RFC 9728 section 3.1
inserts the well-known segment between the host and the resource path, and
:data:`PROTECTED_RESOURCE_ROOT` is the root-level fallback clients that drop
the path also probe.

**No version segment.** ``/mcp`` is the MCP SDK's own default mount for
``streamable_http_app()`` and the path every client's documentation spells, so
an agent's configuration is the vault URL plus ``/mcp`` and nothing else. MCP
negotiates its protocol revision inside ``initialize``; a ``/v1``-style segment
would version the same thing twice and let the two disagree.
"""

from __future__ import annotations

from typing import Final

MCP_PATH: Final[str] = "/mcp"
"""The one path the MCP streamable-HTTP surface answers on a vault."""

_MCP_SUBTREE: Final[str] = f"{MCP_PATH}/"
"""The prefix of every path beneath :data:`MCP_PATH`."""


def in_mcp_realm(path: object) -> bool:
    """Return whether *path* is ``/mcp`` itself or anything beneath it.

    Typed as ``object`` because an ASGI ``scope["path"]`` reaches the edge
    before any framework has validated it. Only a :class:`str` can match: a
    lookalike prefix such as ``/mcpx`` is a different path, not a sibling.

    Args:
        path: The candidate request path.

    Returns:
        ``True`` for :data:`MCP_PATH` and every path under ``/mcp/``.
    """
    return isinstance(path, str) and (path == MCP_PATH or path.startswith(_MCP_SUBTREE))


PROTECTED_RESOURCE_ROOT: Final[str] = "/.well-known/oauth-protected-resource"
"""The RFC 9728 well-known segment, and the root-level metadata fallback."""

PROTECTED_RESOURCE_PATH: Final[str] = f"{PROTECTED_RESOURCE_ROOT}{MCP_PATH}"
"""Where the metadata for the resource at :data:`MCP_PATH` is published."""

PROTECTED_RESOURCE_PATHS: Final[frozenset[str]] = frozenset(
    {PROTECTED_RESOURCE_PATH, PROTECTED_RESOURCE_ROOT}
)
"""Every path a vault answers with its protected-resource metadata."""


def in_metadata_realm(path: object) -> bool:
    """Return whether *path* is one of the protected-resource metadata paths.

    Exact matches only, typed as ``object`` for the reason
    :func:`in_mcp_realm` is: the edge reads ``scope["path"]`` before any
    framework has validated it.

    Args:
        path: The candidate request path.

    Returns:
        ``True`` for a member of :data:`PROTECTED_RESOURCE_PATHS`.
    """
    return isinstance(path, str) and path in PROTECTED_RESOURCE_PATHS
