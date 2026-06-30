"""creek-tools MCP server bootstrap (FEAT-010/011).

Stdio transport per the FEAT-010 pre-decided choice. Five read tools
landed in FEAT-010; FEAT-011 adds seven write tools — ``creek.save``,
``creek.ingest``, ``creek.classify``, ``creek.link``, ``creek.report``,
``creek.skills.refresh``, and ``creek.compile``. All share the same
audit-log + tier-ceiling substrate.

The bootstrap is a single function so it can be exercised by unit
tests (``build_server``) and serve as the ``creek-tools-mcp`` entry
point (``main``).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from creek.config import CONFIG_PATH_ENV_VAR, load_config
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import (
    author_tool,
    classify_tool,
    compile_tool,
    handshake_tool,
    ingest_tool,
    link_tool,
    lint_tool,
    mine_tool,
    purge_classifications_tool,
    purge_daterange_tool,
    purge_fragment_tool,
    purge_source_tool,
    purge_vault_tool,
    redact_scan_tool,
    reflect_tool,
    report_tool,
    save_tool,
    skills_refresh_tool,
    state_read_tool,
    state_render_tool,
)
from creek_mcp.tools.draft import draft_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.models import PrivacyTier
    from creek_mcp.tools.reflect import _LLM, _LLMFactory

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


def _build_compile_llm() -> Callable[[str], str]:
    """Return the compile-side LLM callable, mirroring ``creek compile``.

    Lazy import so the server still boots when the LLM provider is not
    configured; only ``creek.compile`` then fails. Calls into the CLI's
    private factory rather than re-deriving the configuration so the
    behaviour stays in lock-step with ``creek compile``.
    """
    from creek.compile.engine import _default_llm as _engine_default_llm

    return _engine_default_llm(load_config().llm)


def _build_reflect_llm_factory() -> _LLMFactory:
    """Return a tier-keyed LLM factory for ``creek.reflect``.

    The returned ``factory(tier)`` resolves the ``generation`` stage through the
    config's :class:`~creek.classify.llm.router.ModelRouter` for *tier*, so an
    INTIMATE reflection is forced onto the local ``default`` model (or the router
    raises ``IntimateRoutingError`` rather than egressing). Lazy imports keep the
    server bootable without a provider — only a ``creek.reflect`` call then fails,
    surfacing as a structured refusal.
    """
    from creek.classify.llm.providers import build_provider

    router = load_config().model_router

    def _factory(tier: PrivacyTier) -> _LLM:
        cfg = router.resolve("generation", tier)
        provider = build_provider(cfg)
        if not provider.available:
            msg = (
                "LLM provider unavailable for reflection. "
                "Check Ollama or ANTHROPIC_API_KEY configuration."
            )
            raise RuntimeError(msg)
        return lambda prompt: provider.complete(prompt).text

    return _factory


def build_server(
    *,
    vault_path: Path | None = None,
    draft_llm_factory: Callable[[], Callable[[str], str]] | None = None,
    compile_llm_factory: Callable[[], Callable[[str], str]] | None = None,
    reflect_llm_factory: Callable[[], _LLMFactory] | None = None,
) -> FastMCP:
    """Construct a :class:`FastMCP` instance with all FEAT-010/011 tools.

    Args:
        vault_path: Override vault root. Defaults to
            ``load_config().vault_path`` so the MCP surface honours the
            same configuration as the CLI.
        draft_llm_factory: Optional factory for the draft LLM. The
            factory is invoked lazily so only ``creek.draft`` needs an
            LLM provider.
        compile_llm_factory: Optional factory for the compile LLM.
            Invoked lazily so only ``creek.compile`` needs an LLM
            provider.
        reflect_llm_factory: Optional thunk returning the tier-keyed LLM
            factory for ``creek.reflect``. Invoked lazily per call so only
            ``creek.reflect`` needs an LLM provider.
    """
    server: FastMCP = FastMCP(SERVER_NAME)
    vault = _resolve_vault(vault_path)
    consumer = _consumer_from_env()
    factory = draft_llm_factory or _build_draft_llm
    compile_factory = compile_llm_factory or _build_compile_llm
    reflect_factory = reflect_llm_factory or _build_reflect_llm_factory

    @server.tool(name="creek.handshake")
    async def _handshake(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Negotiate vault presence, versions, tier model, and capabilities."""
        tools = await server.list_tools()
        return handshake_tool(
            vault_path=vault,
            capabilities=sorted(tool.name for tool in tools),
            server_name=SERVER_NAME,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.reflect")
    def _reflect(
        content: str | None = None,
        entry_ref: str | None = None,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Return anchored Higher-Self margin notes on a single journal entry."""
        return reflect_tool(
            vault_path=vault,
            llm_factory=reflect_factory(),
            content=content,
            entry_ref=entry_ref,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

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

    @server.tool(name="creek.author")
    def _author(
        query: str,
        medium: str = "research",
        max_rounds: int | None = None,
        dry_run: bool = False,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Author a draft for a query via the Writing Desk (stub shape)."""
        return author_tool(
            vault_path=vault,
            query=query,
            medium=medium,
            max_rounds=max_rounds,
            dry_run=dry_run,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.save")
    def _save(
        target: str,
        body: str,
        title: str | None = None,
        tier: str = "open",
        provenance: list[str] | None = None,
        source_kind: str = "mcp",
        source_id: str | None = None,
        saved_by: str = "mcp",
        full_body: bool = False,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Save a Discord/Claude answer back into the vault."""
        return save_tool(
            vault_path=vault,
            target=target,
            body=body,
            title=title,
            tier=tier,
            provenance=provenance,
            source_kind=source_kind,
            source_id=source_id,
            saved_by=saved_by,
            full_body=full_body,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.ingest")
    def _ingest(
        source_type: str,
        input_path: str,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Ingest a single source into the vault."""
        return ingest_tool(
            vault_path=vault,
            source_type=source_type,
            input_path=input_path,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.redact.scan")
    def _redact_scan(
        input_path: str,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Read-only PII / secret scan over a vault-relative directory (FEAT-027)."""
        return redact_scan_tool(
            vault_path=vault,
            input_path=input_path,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.classify")
    def _classify(
        method: str = "rules",
        force: bool = False,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Re-classify existing fragments via rules or LLM."""
        return classify_tool(
            vault_path=vault,
            method=method,
            force=force,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.link")
    def _link(
        method: str = "embeddings",
        rebuild: bool = False,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Run a single linker stage."""
        return link_tool(
            vault_path=vault,
            method=method,
            rebuild=rebuild,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.report")
    def _report(
        report_type: str = "tags",
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Generate a vault-state report (``tags`` or ``voice``)."""
        return report_tool(
            vault_path=vault,
            report_type=report_type,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.skills.refresh")
    def _skills_refresh(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Regenerate the voice-skill tree."""
        return skills_refresh_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.compile")
    def _compile(
        fragment_ids: list[str],
        target_kind: str,
        target_id: str,
        target_title: str,
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Roll fragments up into a compiled-layer page (FEAT-003)."""
        return compile_tool(
            vault_path=vault,
            fragment_ids=fragment_ids,
            target_kind=target_kind,
            target_id=target_id,
            target_title=target_title,
            llm_factory=compile_factory,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )

    @server.tool(name="creek.purge.fragment")
    def _purge_fragment(
        fragment_id: str,
        auth_token: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one fragment by ID (elevated authorization required)."""
        return purge_fragment_tool(
            vault_path=vault,
            fragment_id=fragment_id,
            auth_token=auth_token,
            dry_run=dry_run,
            consumer=consumer,
        )

    @server.tool(name="creek.purge.source")
    def _purge_source(
        source_type: str,
        auth_token: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete every fragment from *source_type* (elevated auth required)."""
        return purge_source_tool(
            vault_path=vault,
            source_type=source_type,
            auth_token=auth_token,
            dry_run=dry_run,
            consumer=consumer,
        )

    @server.tool(name="creek.purge.classifications")
    def _purge_classifications(
        auth_token: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reset classification metadata vault-wide (elevated auth required)."""
        return purge_classifications_tool(
            vault_path=vault,
            auth_token=auth_token,
            dry_run=dry_run,
            consumer=consumer,
        )

    @server.tool(name="creek.purge.daterange")
    def _purge_daterange(
        start: str,
        end: str,
        auth_token: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete fragments created in ``[start, end]`` (elevated auth required)."""
        return purge_daterange_tool(
            vault_path=vault,
            start=start,
            end=end,
            auth_token=auth_token,
            dry_run=dry_run,
            consumer=consumer,
        )

    @server.tool(name="creek.purge.vault")
    def _purge_vault(
        confirm_vault_path: str | None = None,
        auth_token: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Destroy all vault content (elevated auth + path confirmation)."""
        return purge_vault_tool(
            vault_path=vault,
            confirm_vault_path=confirm_vault_path,
            auth_token=auth_token,
            dry_run=dry_run,
            consumer=consumer,
        )

    return server


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``creek-tools-mcp`` entry point.

    Exposed as a helper so tests can drive parsing without invoking the
    stdio loop.

    Returns:
        A configured :class:`argparse.ArgumentParser` accepting an
        optional ``--config <path>`` flag.
    """
    parser = argparse.ArgumentParser(
        prog="creek-tools-mcp",
        description=(
            "Creek MCP server (stdio transport). Pass --config to "
            "pin a config file regardless of the working directory "
            "the server is launched from."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to creek_config.yaml. When supplied, sets "
            f"{CONFIG_PATH_ENV_VAR} in the process environment so every "
            "tool handler picks it up regardless of cwd."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server over stdio (entry point for ``creek-tools-mcp``).

    Args:
        argv: Optional list of command-line arguments. When ``None``
            (the default for the production entry point), the parser
            reads :data:`sys.argv`. Tests supply an explicit list to
            avoid mutating the process state.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.config is not None:
        if not args.config.exists():
            parser.error(f"--config: file not found: {args.config}")
        os.environ[CONFIG_PATH_ENV_VAR] = str(args.config.resolve())
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    main()
