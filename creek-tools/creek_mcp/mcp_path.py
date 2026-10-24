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
