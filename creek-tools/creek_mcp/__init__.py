"""creek-tools MCP server package (FEAT-010).

Exposes the read-only ``creek`` CLI surface as MCP tools so CrawDad and
the developer's Claude Code consume the same vault interface with
privacy-tier ceiling enforcement at the protocol boundary. Ships the
five read tools — ``state.read``, ``state.render``, ``lint``, ``mine``,
``draft`` — and the audit + tier-ceiling substrate that FEAT-011's
write tools and FEAT-012's purge tools will share.
"""

from creek_mcp.tier_ceiling import TierCeiling, TierCeilingViolationError

__all__ = ["TierCeiling", "TierCeilingViolationError"]
