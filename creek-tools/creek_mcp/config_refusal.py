"""One refusal shape for a served vault config that cannot load (#2096, #2097).

Every MCP tool that reads the served vault's ``creek_config.yaml`` can meet a
config that does not load — a stale ``CREEK_CONFIG``, a document the schema
rejects, or one YAML cannot parse (:data:`creek.config.CONFIG_LOAD_FAILURES`).
Each of those exceptions' messages names something a client must never be
handed: the config path, the parser's quoted snippet of the file, or the
forbidden key and its value. Left uncaught, FastMCP served that text as
``isError``; on ``/v1`` it was a ``500``.

So a tool that catches the failure answers ``<surface> unavailable:
<TypeName>`` — the shape ``creek.reflect`` gave the same fault in #2091 — and
records one WARNING here carrying the tool and the type name only: no message
and no traceback, since either carries the same text the response withholds.
The record lives on this module's logger rather than each tool's so an
operator has one place to look for "the served config does not load".

:func:`is_config_unavailable` is the matching read side for an adapter that
maps a reason to a wire code: a creek-owned prefix *and* a closed type-name
tail, so free text that merely starts with the prefix still fails closed.

This module imports only :mod:`creek_mcp.tier_ceiling`, so a tool importing it
adds no edge into :mod:`creek_mcp.httpapi`.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Final

from creek_mcp.tier_ceiling import refusal_response

if TYPE_CHECKING:
    from collections.abc import Iterable

    from creek_mcp.tier_ceiling import TierCeiling

logger = logging.getLogger(__name__)

TYPE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
"""A bare Python class name — the only tail a config-unavailable reason carries.

Bounded so a reason is never an unbounded string, and closed so nothing but an
identifier (no path separator, space, quote or newline) can follow the prefix.
"""


def config_unavailable_refusal(
    *, tool: str, ceiling: TierCeiling, prefix: str, exc: Exception
) -> dict[str, Any]:
    """Log one type-name-only record and return *tool*'s config refusal.

    Args:
        tool: The refusing tool's name, echoed by the refusal and the record.
        ceiling: The caller's tier ceiling, echoed by the refusal.
        prefix: The tool's ``"<surface> unavailable: "`` reason prefix.
        exc: What the config load raised. Only its type name is used.

    Returns:
        The structured ``refused`` response, reason ``prefix + TypeName``.
    """
    type_name = type(exc).__name__
    # Type name only: the message names the path, a YAML snippet or the key.
    logger.warning("%s refused on an unloadable vault config: %s", tool, type_name)
    return refusal_response(tool=tool, ceiling=ceiling, reason=prefix + type_name)


def is_config_unavailable(reason: str, prefixes: Iterable[str]) -> bool:
    """Return whether *reason* is a config refusal under one of *prefixes*.

    Args:
        reason: A tool refusal's ``reason``.
        prefixes: The ``"<surface> unavailable: "`` prefixes the caller maps.

    Returns:
        ``True`` only when *reason* is exactly one prefix followed by a bare
        type name matching :data:`TYPE_NAME_PATTERN`.
    """
    return any(
        reason.startswith(prefix)
        and TYPE_NAME_PATTERN.fullmatch(reason[len(prefix) :]) is not None
        for prefix in prefixes
    )
