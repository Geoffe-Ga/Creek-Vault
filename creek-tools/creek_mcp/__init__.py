"""creek-tools MCP server package (FEAT-010).

Exposes the read-only ``creek`` CLI surface as MCP tools so CrawDad and
the developer's Claude Code consume the same vault interface with
privacy-tier ceiling enforcement at the protocol boundary. This PR
(part 1 of 2) ships the ``state.read`` / ``state.render`` tools and
the audit + tier-ceiling substrate; the ``lint`` / ``mine`` / ``draft``
tools land in the follow-up.
"""

from creek_mcp.tier_ceiling import TierCeiling, TierCeilingViolationError

__all__ = ["TierCeiling", "TierCeilingViolationError"]
