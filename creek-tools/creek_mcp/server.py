"""creek-tools MCP server bootstrap (FEAT-010).

Stdio transport per the FEAT-010 pre-decided choice. Five read tools
land in this PR — ``creek.state.read``, ``creek.state.render``,
``creek.lint``, ``creek.mine``, and ``creek.draft`` — plus the
audit-log + tier-ceiling substrate they share.

The bootstrap is a single function so it can be exercised by unit
tests (``build_server``) and serve as the ``creek-tools-mcp`` entry
point (``main``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from creek.config import load_config
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import (
    lint_tool,
    mine_tool,
    state_read_tool,
    state_render_tool,
)
from creek_mcp.tools.draft import draft_tool

if TYPE_CHECKING:
    from collections.abc import Callable
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


def _build_draft_llm() -> Callable[[str], str]:
    """Return the production LLM callable, mirroring ``creek draft``.

    Imported lazily so an absent LLM provider only fails the ``draft``
    invocation, not the whole server startup. ``state``/``lint``/``mine``
    must remain callable on hosts without an Anthropic key or running
    Ollama.
    """
    from creek.classify.llm import LLMClassifier

    config = load_config()
    classifier = LLMClassifier(config.llm)
    if not classifier.available:
        msg = (
            "LLM provider unavailable; cannot generate draft. "
            "Check Ollama or ANTHROPIC_API_KEY configuration."
        )
        raise RuntimeError(msg)
    return classifier.invoke_prompt


def build_server(
    *,
    vault_path: Path | None = None,
    draft_llm_factory: Callable[[], Callable[[str], str]] | None = None,
) -> FastMCP:
    """Construct a :class:`FastMCP` instance with all five FEAT-010 tools.

    Args:
        vault_path: Override vault root. Defaults to
            ``load_config().vault_path`` so the MCP surface honours the
            same configuration as the CLI.
        draft_llm_factory: Optional factory for the draft LLM. The
            factory is invoked lazily so only ``creek.draft`` needs an
            LLM provider.
    """
    server: FastMCP = FastMCP(SERVER_NAME)
    vault = _resolve_vault(vault_path)
    consumer = _consumer_from_env()
    factory = draft_llm_factory or _build_draft_llm

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

    @server.tool(name="creek.lint")
    def _lint(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
        checks: list[str] | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Run the unified hygiene lint pass."""
        return lint_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            checks=checks,
            since=since,
            consumer=consumer,
        )

    @server.tool(name="creek.mine")
    def _mine(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
        phase: str = "unclassified",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Mine essay seeds from the vault."""
        return mine_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            phase=phase,
            limit=limit,
            consumer=consumer,
        )

    @server.tool(name="creek.draft")
    def _draft(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
        phase: str = "unclassified",
        index: int = 0,
    ) -> dict[str, Any]:
        """Generate an essay draft from a mined idea."""
        return draft_tool(
            vault_path=vault,
            llm=factory(),
            privacy_tier_ceiling=privacy_tier_ceiling,
            phase=phase,
            index=index,
            consumer=consumer,
        )

    return server


def main() -> None:
    """Run the MCP server over stdio (entry point for ``creek-tools-mcp``)."""
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    main()
