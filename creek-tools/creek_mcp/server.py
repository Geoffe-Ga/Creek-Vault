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
import ipaddress
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from creek.care.guardrail import acute_distress_guard
from creek.config import CONFIG_PATH_ENV_VAR, load_config
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    ConsumerTokenVerifier,
    load_consumer_tokens,
    remote_auth_settings,
)
from creek_mcp.tier_ceiling import TierCeiling, refusal_response
from creek_mcp.tools import (
    author_tool,
    classify_tool,
    compile_tool,
    handshake_tool,
    ingest_tool,
    journal_ingest_tool,
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
    wheel_tool,
)
from creek_mcp.tools.draft import draft_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.types import ContentBlock

    from creek.models import PrivacyTier
    from creek_mcp.tools.reflect import _LLM, _LLMFactory

SERVER_NAME = "creek-tools-mcp"

# Tier ceilings a REMOTE (network-authed) consumer may request. INTIMATE and ALL
# are excluded so intimate content can never be reached over the network — the
# load-bearing boundary of #759. Local (stdio) callers are unaffected.
_REMOTE_ADMITTED_CEILINGS: frozenset[TierCeiling] = frozenset(
    {TierCeiling.OPEN, TierCeiling.PERSONAL}
)


def _current_access_token() -> AccessToken | None:
    """Return the current request's authenticated token, or ``None`` for stdio.

    A thin, monkeypatchable wrapper over the SDK's request-scoped
    ``get_access_token`` — non-``None`` exactly when the call arrived over the
    authenticated network transport.
    """
    return get_access_token()


def _effective_consumer(default: str) -> str:
    """Return the per-call consumer: the authed remote ``client_id``, else *default*.

    So a network call is audited under the consumer its bearer token identifies,
    while a local stdio call keeps the process-global ``CREEK_MCP_CONSUMER``.
    """
    token = _current_access_token()
    return token.client_id if token is not None else default


class _BoundedFastMCP(FastMCP):
    """``FastMCP`` that enforces the remote tier-ceiling cap at the one call chokepoint.

    Every tool call flows through :meth:`call_tool`. When the call is remote (an
    authenticated network request — ``_current_access_token()`` is set), a
    requested ceiling above the remote cap (i.e. ``INTIMATE`` or ``ALL``, or any
    unrecognised value) is refused **before dispatch**, so intimate content is
    never even read for a remote caller. Local stdio calls (no token) pass
    through unchanged — the default per-tool ``OPEN`` ceiling still applies.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        """Refuse over-ceiling remote calls, else dispatch normally.

        Note: this ceiling gate is a **no-op for the ``creek.purge.*`` tools** —
        they declare no ``privacy_tier_ceiling`` parameter, so ``arguments.get``
        below always falls back to ``OPEN`` (admitted) for them. That is
        intentional: purge is not tier-scoped and is instead gated, fail-closed,
        by ``CREEK_MCP_ELEVATED_TOKEN`` (see ``creek_mcp.auth.is_elevated`` and the
        purge tools), which a per-consumer bearer alone does not satisfy. Do not
        rely on this gate for purge protection.
        """
        if _current_access_token() is not None:
            raw = arguments.get("privacy_tier_ceiling", TierCeiling.OPEN.value)
            try:
                requested = TierCeiling(raw)
            except ValueError:
                requested = None
            if requested not in _REMOTE_ADMITTED_CEILINGS:
                return refusal_response(
                    tool=name,
                    ceiling=TierCeiling.OPEN,
                    reason=(
                        "remote consumers may not request a ceiling above "
                        f"'{TierCeiling.PERSONAL.value}'; intimate content is not "
                        "reachable over the network"
                    ),
                )
        return await super().call_tool(name, arguments)


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
    token_verifier: TokenVerifier | None = None,
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
        token_verifier: When set, the server serves the network transport
            authenticated — every request must present a bearer token the
            verifier accepts (no anonymous access), and remote calls are
            capped below INTIMATE. ``None`` (the default) is the local stdio
            server, unauthenticated as before.
    """
    server: FastMCP = (
        _BoundedFastMCP(
            SERVER_NAME, token_verifier=token_verifier, auth=remote_auth_settings()
        )
        if token_verifier is not None
        else _BoundedFastMCP(SERVER_NAME)
    )
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
            consumer=_effective_consumer(consumer),
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
            care_guard=acute_distress_guard,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=_effective_consumer(consumer),
        )

    @server.tool(name="creek.wheel")
    def _wheel(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Return a per-frequency balance read of the corpus for the Map."""
        return wheel_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=_effective_consumer(consumer),
        )

    @server.tool(name="creek.journal")
    def _journal(
        content: str,
        external_id: str,
        timestamp: str | None = None,
        tier: str = "open",
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Ingest one Adepthood journal entry as a vault fragment (idempotent)."""
        return journal_ingest_tool(
            vault_path=vault,
            content=content,
            external_id=external_id,
            timestamp=timestamp,
            tier=tier,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=_effective_consumer(consumer),
        )

    @server.tool(name="creek.state.read")
    def _state_read(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Return the latest 00-Creek-Meta/State/latest.md content."""
        return state_read_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=_effective_consumer(consumer),
        )

    @server.tool(name="creek.state.render")
    def _state_render(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Re-render the audit report (the expensive path)."""
        return state_render_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
        )

    @server.tool(name="creek.skills.refresh")
    def _skills_refresh(
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Regenerate the voice-skill tree."""
        return skills_refresh_tool(
            vault_path=vault,
            privacy_tier_ceiling=privacy_tier_ceiling,
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
            consumer=_effective_consumer(consumer),
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
    parser.add_argument(
        "--transport",
        choices=("stdio", "network"),
        default="stdio",
        help=(
            "stdio (default) for local consumers, or network (authenticated "
            "streamable-http) for a remote consumer. Network mode requires "
            f"{CONSUMER_TOKENS_ENV} (no anonymous access) and caps remote "
            "consumers below INTIMATE."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Network transport bind host. A non-loopback host requires "
            "--tls-cert/--tls-key so bearer tokens never transit in cleartext."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Network transport bind port."
    )
    parser.add_argument(
        "--tls-cert",
        type=Path,
        default=None,
        help=(
            "Path to the TLS certificate (PEM). Required together with "
            "--tls-key when binding a non-loopback host."
        ),
    )
    parser.add_argument(
        "--tls-key",
        type=Path,
        default=None,
        help=(
            "Path to the TLS private key (PEM). Required together with "
            "--tls-cert when binding a non-loopback host."
        ),
    )
    return parser


def _is_loopback(host: str) -> bool:
    """Return whether *host* is a loopback bind (safe for plaintext transport).

    Loopback traffic never leaves the machine, so serving bearer-token auth
    without TLS is acceptable there — and only there.

    Args:
        host: The ``--host`` value: an IP literal or a hostname.

    Returns:
        ``True`` for loopback IPs (``127.0.0.0/8``, ``::1``) and the literal
        ``"localhost"`` (case-insensitive); ``False`` for everything else,
        including ``""``, wildcard binds, other hostnames, and strings that
        do not parse as an IP address at all.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal (hostname, empty, garbage): assume routable.
        return False


def _require_transport_confidentiality(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Refuse network configurations that would put bearer tokens on the wire.

    Enforces, in order: ``--tls-cert``/``--tls-key`` come as a pair; both
    files exist on disk; and a non-loopback ``--host`` is only served when
    TLS is configured. Each violation exits via :meth:`argparse.ArgumentParser.error`
    (nonzero exit, message on stderr) *before* any socket is opened (#837).

    Args:
        parser: The CLI parser, used to report errors in argparse style.
        args: Parsed arguments carrying ``host``, ``tls_cert``, ``tls_key``.
    """
    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error(
            "--tls-cert and --tls-key are required together; supply both "
            "(or neither, for a loopback-only bind)"
        )
    if args.tls_cert is not None:
        for flag, path in (("--tls-cert", args.tls_cert), ("--tls-key", args.tls_key)):
            if not path.exists():
                parser.error(f"{flag}: file not found: {path}")
        return
    if not _is_loopback(args.host):
        parser.error(
            f"refusing to serve on non-loopback host {args.host!r} without TLS: "
            "bearer tokens would transit the network in cleartext. Bind "
            "127.0.0.1, terminate TLS in a reverse proxy, or pass "
            "--tls-cert/--tls-key"
        )


def _serve_network(server: FastMCP, args: argparse.Namespace) -> None:
    """Serve the streamable-http transport, with TLS when cert + key are given.

    With ``--tls-cert``/``--tls-key`` configured, runs the server's Starlette
    app under uvicorn with ``ssl_certfile``/``ssl_keyfile`` (mirroring the
    SDK's own ``run_streamable_http_async`` bootstrap, plus TLS). Without
    TLS — a loopback bind, per :func:`_require_transport_confidentiality` —
    falls back to the SDK's plain ``run("streamable-http")``.

    Args:
        server: The built (authenticated) :class:`FastMCP` server.
        args: Parsed arguments carrying ``host``, ``port``, ``tls_cert``,
            ``tls_key``.
    """
    if args.tls_cert is not None and args.tls_key is not None:
        # Lazy import, matching FastMCP's own transport bootstrap pattern.
        import uvicorn

        config = uvicorn.Config(
            server.streamable_http_app(),
            host=args.host,
            port=args.port,
            log_level=server.settings.log_level.lower(),
            ssl_certfile=str(args.tls_cert),
            ssl_keyfile=str(args.tls_key),
        )
        uvicorn.Server(config).run()
    else:
        server.run(transport="streamable-http")


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server over stdio (local) or the authenticated network transport.

    The network branch is fail-closed twice over: it refuses to start without
    per-consumer tokens (no anonymous access), and it refuses a non-loopback
    bind unless TLS is configured (no cleartext bearer tokens, #837).

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

    if args.transport == "network":
        tokens = load_consumer_tokens()
        if not tokens:
            # No anonymous access: refuse to expose the vault without per-consumer
            # tokens, rather than serving unauthenticated.
            parser.error(
                f"network transport requires {CONSUMER_TOKENS_ENV} "
                "(consumer=token pairs); refusing to serve without authentication"
            )
        _require_transport_confidentiality(parser, args)
        server = build_server(token_verifier=ConsumerTokenVerifier(tokens))
        server.settings.host = args.host
        server.settings.port = args.port
        _serve_network(server, args)
    else:
        build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    main()
