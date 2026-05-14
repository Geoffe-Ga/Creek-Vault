"""A minimal MCP stdio server used by the CrawDad client tests.

Exposes one no-op tool, ``echo``, so the client's ``connect`` and
``list_tools`` paths can be exercised without depending on the full
``creek-tools-mcp`` package being installed in the test environment.

Run as ``python -m tests.fixtures.fake_mcp_server`` (stdio transport).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def build() -> FastMCP:
    """Return a fresh :class:`FastMCP` with a single ``echo`` tool."""
    server: FastMCP = FastMCP("crawdad-test-mcp")

    @server.tool(name="echo")
    def _echo(message: str = "hi") -> dict[str, str]:
        """Return *message* unchanged so callers can verify round-tripping."""
        return {"reply": message}

    return server


def main() -> None:
    """Run the fixture server over stdio."""
    build().run(transport="stdio")


if __name__ == "__main__":
    main()
