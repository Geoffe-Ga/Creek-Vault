"""creek-tools MCP server bootstrap (FEAT-010 part 1 of 2).

Stdio transport per the FEAT-010 pre-decided choice. This PR ships the
``creek.state.read`` and ``creek.state.render`` tools plus the
audit-log + tier-ceiling substrate that every read tool will share.
``creek.lint``, ``creek.mine``, and ``creek.draft`` arrive in the
follow-up PR (FEAT-010 part 2) once this skeleton has merged.

The bootstrap is a single function so it can be exercised by a unit
test (``build_server``) and also serve as the ``creek-tools-mcp``
entry point (``main``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from creek.config import load_config
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import state_read_tool, state_render_tool

if TYPE_CHECKING:
    from pathlib import Path

SERVER_NAME = "creek-tools-mcp"


def _resolve_vault(vault_path: Path | None) -> Path:
    """Return the supplied path or fall back to ``load_config().vault_path``."""
    if vault_path is not None:
        return vault_path
    return load_config().vault_path


def _consumer_from_env() -> str:
    """Return the consumer identifier from ``CREEK_MCP_CONSUMER`` or unknown."""
    return os.environ.get("CREEK_MCP_CONSUMER", "unknown")


def build_server(*, vault_path: Path | None = None) -> FastMCP:
    """Construct a :class:`FastMCP` instance with the FEAT-010 part-1 tools.

    Args:
        vault_path: Override vault root. Defaults to
            ``load_config().vault_path`` so the MCP surface honours the
            same configuration that drives the CLI.
    """
    server: FastMCP = FastMCP(SERVER_NAME)
    vault = _resolve_vault(vault_path)
    consumer = _consumer_from_env()

    @server.tool(name="creek.state.read")
    def _state_read(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Return the latest 00-Creek-Meta/State/latest.md content."""
        return state_read_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.state.render")
    def _state_render(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Re-render the audit report (the expensive path)."""
        return state_render_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    return server


def main() -> None:
    """Run the MCP server over stdio (entry point for ``creek-tools-mcp``)."""
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    main()
