"""creek-tools MCP server bootstrap (FEAT-010/011).

Stdio transport per the FEAT-010 pre-decided choice. Five read tools
landed in FEAT-010; FEAT-011 adds seven write tools — ``creek.save``,
``creek.ingest``, ``creek.classify``, ``creek.link``, ``creek.report``,
``creek.skills.refresh``, and ``creek.compile``. All share the same
audit-log + tier-ceiling substrate. ``creek.upload`` (#1023) is the
Adepthood byte-staging surface: it takes one document's base64 bytes and
ingests them through the same ledger-backed pipeline ``creek.journal``
uses for text.

The bootstrap is a single function so it can be exercised by unit
tests (``build_server``) and serve as the ``creek-tools-mcp`` entry
point (``main``).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from creek.care.guardrail import acute_distress_guard
from creek.config import CONFIG_PATH_ENV_VAR, load_config
from creek_mcp.auth import ELEVATED_TOKEN_ENV
from creek_mcp.policy import (
    Admission,
    CallerIdentity,
    admitted_ceiling,
    effective_consumer,
)
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    ConsumerTokenVerifier,
    load_consumer_tokens,
    remote_auth_settings,
)
from creek_mcp.tier_ceiling import TierCeiling, refusal_response
from creek_mcp.token_policy import require_min_length
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
    upload_tool,
    wheel_tool,
)
from creek_mcp.tools.draft import draft_tool
from creek_mcp.transport_posture import is_loopback, require_transport_confidentiality

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.types import ContentBlock

    from creek.compile.engine import CompileLLM
    from creek.models import PrivacyTier
    from creek_mcp.tools.compile import CompileLLMFactory
    from creek_mcp.tools.draft import DraftLLMFactory
    from creek_mcp.tools.reflect import _LLM, _LLMFactory

SERVER_NAME = "creek-tools-mcp"

DEFAULT_MCP_NETWORK_PORT: Final[int] = 8000
"""Default ``--port`` for the MCP network transport.

Named rather than left as a bare literal so the ``/v1`` HTTP adapter can assert
it picked a *different* one (#1074). The two adapters are meant to run side by
side on one host, so a shared default would make the second one to start fail
with ``address already in use`` — a collision that is trivial to prevent and
annoying to diagnose. The value itself is unchanged.
"""


def _current_access_token() -> AccessToken | None:
    """Return the current request's authenticated token, or ``None`` for stdio.

    A thin, monkeypatchable wrapper over the SDK's request-scoped
    ``get_access_token`` — non-``None`` exactly when the call arrived over the
    authenticated network transport. This is the single SDK seam on the
    identity path: everything below reaches the request context through here
    and nowhere else, which is what lets the tests drive both sides of the
    boundary without standing up a transport.
    """
    return get_access_token()


def _caller_identity() -> CallerIdentity:
    """Answer :mod:`creek_mcp.policy`'s two questions with MCP's facts.

    The MCP half of the #1073 split: policy decides, this states which side of
    the network the call arrived from and whose credential it carried. An
    authenticated network request has an access token whose ``client_id`` is
    the consumer; a local stdio call has neither.

    **Call this per call. Never hoist it.** ``get_access_token()`` is
    request-scoped, while the ``consumer`` that :func:`build_server` resolves
    from the environment is bound once at build time. Building the identity
    beside it would pin every later call to whichever caller happened to be in
    flight first — forging audit attribution, and, from a stdio-built identity,
    uncapping every remote call thereafter. That is the highest-consequence
    failure mode of this arrangement, and ``tests/test_mcp_remote.py`` pins it
    by driving two bearers through one built server.
    """
    token = _current_access_token()
    if token is None:
        return CallerIdentity(consumer=None, is_remote=False)
    return CallerIdentity(consumer=token.client_id, is_remote=True)


def _effective_consumer(default: str) -> str:
    """Return the per-call consumer: the authed remote ``client_id``, else *default*.

    So a network call is audited under the consumer its bearer token identifies,
    while a local stdio call keeps the process-global ``CREEK_MCP_CONSUMER``.
    The rule itself is :func:`creek_mcp.policy.effective_consumer`; all this
    adds is MCP's answer to "who is calling", rebuilt for the request in flight.
    Kept module-level under this exact name because the 24 tool closures below
    call it on every request.
    """
    return effective_consumer(_caller_identity(), default)


class _BoundedFastMCP(FastMCP):
    """The MCP adapter for :mod:`creek_mcp.policy`'s remote tier-ceiling cap.

    Every tool call flows through :meth:`call_tool`, the one MCP-side chokepoint
    at which the requested ceiling is still a raw argument. The cap is not
    decided here: since #1073 it is adapter-independent policy in
    :func:`creek_mcp.policy.admitted_ceiling`, so the ``/v1`` HTTP surface
    reaches the same verdict without an MCP request context to key off. What
    this class owns is the two things only MCP can answer — *who is the caller*
    and *is this remote*, via :func:`_caller_identity` — plus what a refusal
    looks like on this transport: the
    :func:`creek_mcp.tier_ceiling.refusal_response` payload, where ``/v1``
    deliberately answers ``422 invalid_request`` instead.

    A refused remote call is refused **before dispatch**, so intimate content is
    never even read for it. Local stdio calls pass through unchanged — the
    default per-tool ``OPEN`` ceiling still applies.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        """Refuse over-ceiling remote calls, else dispatch normally.

        The missing-key default is *adapter* vocabulary rather than policy's: a
        tool that declares no ``privacy_tier_ceiling`` is read here as having
        asked for ``OPEN``, exactly as ``/v1`` reads an absent
        ``X-Creek-Tier-Ceiling`` header. Handing the absent key to policy as
        ``None`` would also be safe — it fails closed — but it would refuse the
        purge tools below rather than admitting them.

        Note: this ceiling gate is a **no-op for the ``creek.purge.*`` tools** —
        they declare no ``privacy_tier_ceiling`` parameter, so ``arguments.get``
        below always falls back to ``OPEN`` (admitted) for them. That is
        intentional: purge is not tier-scoped and is instead gated, fail-closed,
        by ``CREEK_MCP_ELEVATED_TOKEN`` (see ``creek_mcp.auth.is_elevated`` and the
        purge tools), which a per-consumer bearer alone does not satisfy. Do not
        rely on this gate — or on :mod:`creek_mcp.policy` — for purge protection.
        """
        raw = arguments.get("privacy_tier_ceiling", TierCeiling.OPEN.value)
        verdict = admitted_ceiling(_caller_identity(), raw)
        # Dispatch on the POSITIVE verdict, never on "not a refusal". If policy
        # ever grows a third verdict, this falls to the refusal branch instead
        # of silently uncapping the call — and because the branch below then
        # narrows to a type that has no ``reason``, mypy fails the build rather
        # than leaving the regression to be noticed at runtime.
        if isinstance(verdict, Admission):
            return await super().call_tool(name, arguments)
        # ``ceiling=OPEN`` rather than the requested one: the refusal echoes
        # the transport's own default, never the value that was refused.
        return refusal_response(
            tool=name,
            ceiling=TierCeiling.OPEN,
            reason=verdict.reason,
        )


def _resolve_vault(vault_path: Path | None) -> Path:
    """Return the supplied path or fall back to ``load_config().vault_path``."""
    if vault_path is not None:
        return vault_path
    return load_config().vault_path


def _consumer_from_env() -> str:
    """Return the consumer identifier from ``CREEK_MCP_CONSUMER`` or unknown."""
    return os.environ.get("CREEK_MCP_CONSUMER", "unknown")


def _build_draft_llm(tier: PrivacyTier) -> Callable[[str], str]:
    """Return the draft-side LLM callable, routed for *tier* (#958).

    Mirrors :func:`_build_compile_llm`: the ``generation`` stage is resolved
    through the config's :class:`~creek.classify.llm.router.ModelRouter`, so
    an INTIMATE draft is forced onto the local ``default`` model — or the
    router raises ``IntimateRoutingError`` rather than egressing the source
    ids and titles the draft prompt carries. Nothing here re-checks the tier
    or picks a provider: ``draft_tool`` derives the routing tier from the
    seed's source fragments and this hands it to the one chokepoint that owns
    the decision.

    Lazy imports keep the server bootable with no provider configured — only
    a ``creek.draft`` call then fails, and it fails as a structured refusal.
    ``state``/``lint``/``mine`` stay callable on hosts with neither an
    Anthropic key nor a running Ollama.

    Args:
        tier: The routing tier ``draft_tool`` computed from the caller's
            ceiling and the seed's source fragments' own classifications.

    Returns:
        A prompt → completion-text callable bound to the resolved provider.

    Raises:
        RuntimeError: When the resolved provider is unavailable, or — as
            :class:`~creek.classify.llm.router.IntimateRoutingError`, a
            subclass — when intimate content has no local backend to fall
            back to. ``draft_tool`` turns either into a refusal.
    """
    from creek.classify.llm.providers import build_provider

    cfg = load_config().model_router.resolve("generation", tier)
    provider = build_provider(cfg)
    if not provider.available:
        msg = (
            "LLM provider unavailable for draft. "
            "Check Ollama or ANTHROPIC_API_KEY configuration."
        )
        raise RuntimeError(msg)
    return lambda prompt: provider.complete(prompt).text


def _build_compile_llm(tier: PrivacyTier) -> CompileLLM:
    """Return the compile-side LLM callable, routed for *tier* (#928/#929).

    Mirrors :func:`_build_reflect_llm_factory`'s inner factory: the
    ``generation`` stage is resolved through the config's
    :class:`~creek.classify.llm.router.ModelRouter`, so an INTIMATE compile is
    forced onto the local ``default`` model — or the router raises
    ``IntimateRoutingError`` rather than egressing the source titles the
    compile prompt carries. Nothing here re-checks the tier or picks a
    provider: the tier arrives already reconciled and this hands it to the one
    chokepoint that owns the decision.

    Lazy imports keep the server bootable with no provider configured — only a
    ``creek.compile`` call then fails, and it fails as a structured refusal.

    Args:
        tier: The routing tier, reconciled from two signals: since #962
            ``creek.compile.engine`` derives the source fragments' own
            classification (it is the layer that loaded them) and
            ``compile_tool`` folds in the caller's declared ceiling on the way
            through. This function sees only the result.

    Returns:
        A prompt → completion-text callable bound to the resolved provider.

    Raises:
        RuntimeError: When the resolved provider is unavailable, or — as
            :class:`~creek.classify.llm.router.IntimateRoutingError`, a
            subclass — when intimate content has no local backend to fall
            back to. ``compile_tool`` turns either into a refusal.
    """
    from creek.classify.llm.providers import build_provider

    cfg = load_config().model_router.resolve("generation", tier)
    provider = build_provider(cfg)
    if not provider.available:
        msg = (
            "LLM provider unavailable for compile. "
            "Check Ollama or ANTHROPIC_API_KEY configuration."
        )
        raise RuntimeError(msg)
    return lambda prompt: provider.complete(prompt).text


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
    draft_llm_factory: DraftLLMFactory | None = None,
    compile_llm_factory: CompileLLMFactory | None = None,
    reflect_llm_factory: Callable[[], _LLMFactory] | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastMCP:
    """Construct a :class:`FastMCP` instance with all FEAT-010/011 tools.

    Args:
        vault_path: Override vault root. Defaults to
            ``load_config().vault_path`` so the MCP surface honours the
            same configuration as the CLI.
        draft_llm_factory: Optional tier-keyed factory for the draft LLM —
            ``factory(tier)``. Passed straight to ``creek.draft``, which
            invokes it lazily (and only once it knows which seed it is
            drafting), so only ``creek.draft`` needs an LLM provider.
        compile_llm_factory: Optional tier-keyed factory for the compile
            LLM — ``factory(tier)``. Passed straight to ``creek.compile``,
            which invokes it lazily (and only after its source-tier gate),
            so only ``creek.compile`` needs an LLM provider.
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

    @server.tool(name="creek.upload")
    def _upload(
        filename: str,
        content_base64: str,
        external_id: str,
        timestamp: str | None = None,
        tier: str = "open",
        privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    ) -> dict[str, Any]:
        """Stage one uploaded document's bytes and ingest it as a vault fragment."""
        return upload_tool(
            vault_path=vault,
            filename=filename,
            content_base64=content_base64,
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
            llm_factory=factory,
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
        """Read-only PII / secret scan of the FEAT-027 staging subtree.

        Scoped to ``00-Creek-Meta/Inbound/``, which every ceiling admits. The
        scan reads no per-file privacy tier, so any other vault path is ranked
        as intimate content and needs privacy_tier_ceiling=intimate or all.
        """
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
        "--port",
        type=int,
        default=DEFAULT_MCP_NETWORK_PORT,
        help="Network transport bind port.",
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


# Back-compat aliases for the posture gate that moved to
# ``creek_mcp.transport_posture`` in #1074, so ``/v1`` and MCP share one
# implementation instead of two that can drift. Kept under the original private
# names because they are reached by name from ``main`` and from
# ``tests/test_mcp_remote.py``; the move is behaviour-preserving, and the proof
# is that not one of those tests needed editing. Retiring the aliases once
# nothing imports them is tracked as a follow-up.
_is_loopback = is_loopback
_require_transport_confidentiality = require_transport_confidentiality


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


def _require_strong_elevated_token(parser: argparse.ArgumentParser) -> None:
    """Refuse to start when the elevated token is configured but too weak (#907).

    ``CREEK_MCP_ELEVATED_TOKEN`` gates irreversible ``creek.purge.*``
    calls, so a guessable value is a startup failure rather than a quietly
    weak gate. An *absent or empty* value is not an error: that is the
    supported "purge disabled" posture, under which
    :func:`creek_mcp.auth.is_elevated` already denies every call.

    Applies to both transports — the token is read from the environment
    at call time, not from the wire — so it is checked before the
    transport branch and before any server is built.

    Args:
        parser: The CLI parser, used to report errors in argparse style
            (usage on stderr, exit code 2).
    """
    configured = os.environ.get(ELEVATED_TOKEN_ENV, "")
    if not configured:
        return
    try:
        require_min_length(ELEVATED_TOKEN_ENV, configured)
    except ValueError as exc:
        # Mirrors the #838 consumer-token treatment: exit with the rotation
        # recipe (never the token value) rather than serve a weak gate.
        parser.error(str(exc))


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server over stdio (local) or the authenticated network transport.

    Startup is fail-closed four times over. On *every* transport it refuses
    an elevated token below the minimum-strength floor (#907). The network
    branch additionally refuses to start without per-consumer tokens (no
    anonymous access), refuses a configured consumer token below the same
    floor (#838), and refuses a non-loopback bind unless TLS is configured
    (no cleartext bearer tokens, #837).

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

    _require_strong_elevated_token(parser)

    if args.transport == "network":
        try:
            tokens = load_consumer_tokens()
        except ValueError as exc:
            # A configured token is below the minimum-strength floor (#838):
            # exit with the rotation recipe rather than serve a weak secret.
            parser.error(str(exc))
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
