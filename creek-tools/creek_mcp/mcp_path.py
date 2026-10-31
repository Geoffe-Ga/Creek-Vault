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

import re
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


ALLOCATION_ROOT: Final[str] = "/a"
"""The edge prefix under which each managed allocation's own resource lives (#2002).

A managed allocation's resource is ``<edge>/a/<slug>/mcp`` (owner decision
R3-1): a path on the shared edge origin, so no new certificate is needed, and a
token audience no other vault names.
"""

ALLOCATION_SLUG_CHARS: Final[int] = 32
"""The exact length of a minted slug: 24 random bytes in unpadded base64url."""

_ALLOCATION_MCP_PATH_RE: Final[re.Pattern[str]] = re.compile(
    rf"{ALLOCATION_ROOT}/[A-Za-z0-9_-]{{{ALLOCATION_SLUG_CHARS}}}{MCP_PATH}"
)


def is_allocation_mcp_path(path: object) -> bool:
    """Return whether *path* is exactly one allocation's ``/a/<slug>/mcp``.

    Args:
        path: The candidate path; anything but a :class:`str` never matches.

    Returns:
        ``True`` only for the minted grammar: no subtree, no nesting, no
        percent-encoding and no slug of any other length.
    """
    return isinstance(path, str) and _ALLOCATION_MCP_PATH_RE.fullmatch(path) is not None


def allocation_mcp_path(slug: str) -> str:
    """Return the per-allocation resource path for *slug*.

    Args:
        slug: The allocation's minted, URL-safe slug.

    Returns:
        :data:`ALLOCATION_ROOT`, the slug, then :data:`MCP_PATH`.

    Raises:
        ValueError: When *slug* is not the exact minted grammar. The message
            never echoes the value.
    """
    path = f"{ALLOCATION_ROOT}/{slug}{MCP_PATH}"
    if not is_allocation_mcp_path(path):
        raise ValueError("allocation slug is invalid")
    return path


def is_resource_path(path: object) -> bool:
    """Return whether *path* may be a vault's MCP resource path.

    Args:
        path: The candidate path.

    Returns:
        ``True`` for :data:`MCP_PATH` — a self-hosted vault and the shared
        edge — and for one allocation's :func:`allocation_mcp_path`.
    """
    return path == MCP_PATH or is_allocation_mcp_path(path)


def allocation_metadata_path(slug: str) -> str:
    """Return where one allocation's RFC 9728 metadata is published (#2003).

    Args:
        slug: The allocation's minted, URL-safe slug.

    Returns:
        :data:`PROTECTED_RESOURCE_ROOT` then :func:`allocation_mcp_path` — the
        RFC 9728 section 3.1 insertion for a path-scoped resource.

    Raises:
        ValueError: When *slug* is not the exact minted grammar.
    """
    return f"{PROTECTED_RESOURCE_ROOT}{allocation_mcp_path(slug)}"


def allocation_path_of_metadata(path: object) -> str | None:
    """Return the ``/a/<slug>/mcp`` a per-allocation metadata *path* describes.

    Args:
        path: The candidate request path, unvalidated.

    Returns:
        The allocation's resource path, or ``None`` unless *path* is exactly
        :func:`allocation_metadata_path` of a minted slug.
    """
    if not isinstance(path, str) or not path.startswith(PROTECTED_RESOURCE_ROOT):
        return None
    resource_path = path.removeprefix(PROTECTED_RESOURCE_ROOT)
    return resource_path if is_allocation_mcp_path(resource_path) else None


def is_allocation_metadata_path(path: object) -> bool:
    """Return whether *path* is exactly one allocation's metadata path.

    Args:
        path: The candidate request path, unvalidated.

    Returns:
        ``True`` only for :func:`allocation_metadata_path` of a minted slug.
    """
    return allocation_path_of_metadata(path) is not None
