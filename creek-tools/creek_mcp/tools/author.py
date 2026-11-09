"""``creek.author`` MCP tool — author via the real Writing Desk (FEAT-041 #460).

A thin adapter: it validates the request, records the audit entry, and
delegates to ``creek.author.run_author`` (or ``plan_author`` for a dry run) —
no desk logic lives in the MCP layer (SPEC §4.3: both the CLI and MCP surfaces
call the same desk). The response carries the draft body, the reflection
verdict, per-claim provenance, and the cited ``claims`` (each with its
``source_fragments``) — **unless** the desk's HARD privacy check condemned the
draft, in which case the verb answers ``status: "refused"`` and withholds all
three content keys (#1353). Returning a ``privacy_compliance`` finding
*alongside* the protected text it just caught hands the caller the very breach
the gate detected; the finding travels, the corpus does not. Existing MCP verbs
are untouched.

A desk failure answers ``status: "error"`` with a **fixed** reason, never the
exception's text (#2082): :data:`AUTHOR_PROVIDER_FAILED_REASON` for a failed
provider call and :data:`AUTHOR_DESK_FAILED_REASON` for anything else. That
text used to be the reason, so a vendor's 4xx message, an Ollama URL and a
server config path all reached the client. The detail now rides only the
traceback of one operator log record. Three residuals are deliberate, as in
``creek.draft``: an unsupported medium echoes its own message (it names the
supported media), the router's
:class:`~creek.classify.llm.router.IntimateRoutingError` keeps its
creek-written text, and a writer's refused key answers the shared
:data:`~creek.classify.llm.providers.REQUEST_KEY_REJECTED_REASON`.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

import frontmatter

from creek.author import plan_author, require_supported_medium, run_author
from creek.author.models import ZERO_EVIDENCE_WARNING, has_zero_evidence
from creek.classify.llm.providers import (
    PROVIDER_CALL_FAILURES,
    REQUEST_KEY_REJECTED_REASON,
    RequestKeyRejectedError,
)
from creek.classify.llm.router import IntimateRoutingError
from creek.generate.drafts import INFERENCE_SOURCE_FRONTMATTER_KEY, reserve_draft_path
from creek.progress import CompositionStage, StageCallback, ignore_stage
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author import AuthoredDraft, ReflectionFinding
    from creek.author.client import AuthorLLMClient
    from creek.author.models import FindingDimension
    from creek.models import PrivacyTier
    from creek_mcp.provenance import InferenceProvenance

TOOL_NAME = "creek.author"

logger = logging.getLogger(__name__)

AUTHOR_PROVIDER_FAILED_REASON: Final[str] = "LLM provider call failed for author"
"""The fixed ``reason`` when the desk's provider call failed (#2082).

Answered for anything in
:data:`~creek.classify.llm.providers.PROVIDER_CALL_FAILURES` other than the two
residuals the module docstring names: a vendor 4xx, an empty body, a dropped
connection, an unreachable Ollama endpoint. The vendor's own message — which
:func:`creek.classify.llm.providers._error_detail` forwards for a 4xx — and an
Ollama URL ride only the operator log's traceback. The sibling of
:data:`creek_mcp.tools.draft.DRAFT_PROVIDER_FAILED_REASON`, worded alike.
"""

AUTHOR_DESK_FAILED_REASON: Final[str] = "writing desk failed for author"
"""The fixed ``reason`` for any other desk failure (#2082).

A stale ``CREEK_CONFIG`` (whose ``FileNotFoundError`` names a server path), a
config pydantic rejects, unparseable YAML (whose message names the file and
quotes it), a missing contract: none of it is the caller's to read, and all of
it reaches the operator log's traceback instead. Distinct from
:data:`AUTHOR_PROVIDER_FAILED_REASON` so a caller can tell "retry later" from
"the vault needs its operator".
"""

AUTHOR_STAGES: Final[tuple[CompositionStage, ...]] = (
    CompositionStage.SOURCES,
    CompositionStage.COMPOSED,
    CompositionStage.VOICE_GUARDED,
)
"""The three boundaries a ``creek.author`` call reports, in order (#1966).

The desk mines no seed and assembles no skill stack, and its synchronous
answer carries the body rather than saving it, so it crosses three of the six
:class:`~creek.progress.CompositionStage` boundaries.
"""

_AUTHORED_DRAFT_STRATEGY: Final[str] = "writing_desk"
"""The ``idea_strategy`` stamped on a draft the desk wrote to the vault."""


# The one reflection dimension whose presence turns a finished draft into a
# refusal (#1353). Annotated with :data:`~creek.author.models.FindingDimension`
# — the ``Literal`` that already contains this exact string — so strict mypy
# rejects a typo here instead of leaving a filter that silently matches nothing
# forever, which would restore the leak without failing anything.
#
# The literal is deliberately re-stated rather than imported from whatever
# module happens to construct the finding (today, the body of
# :func:`creek.author.checks.check_privacy_compliance`) or from the CLI's own
# module-local twin (``creek/cli.py:4954``, landed by #1352): an MCP tool
# reaching into another surface's private internals is a worse coupling than
# one duplicated string that ``FindingDimension`` already type-checks.
# Promoting a single shared public constant next to the check itself is the
# right eventual cleanup and is tracked as issue #1362, not done here.
_PRIVACY_DIMENSION: Final[FindingDimension] = "privacy_compliance"


# The refusal's ``reason``: fixed prose plus a count, and nothing else. No
# fragment id, no draft text, no finding message is interpolated — a refusal
# that quotes the corpus to explain itself has not refused anything. The
# offending ids and tiers ride in ``privacy_findings`` instead, whose messages
# are corpus-free by construction (``creek/author/checks.py:342-346``: fragment
# id and tier only).
_PRIVACY_REFUSAL_REASON: Final[str] = (
    "The Writing Desk's privacy gate condemned this draft "
    "({count} privacy_compliance finding(s)): it reproduces protected text "
    "from a cited fragment held above the medium contract's default privacy "
    "tier. The body, claims and provenance are withheld; see "
    "privacy_findings for the offending fragments and their tiers."
)


class AuthorLLMFactory(Protocol):
    """Tier-keyed builder for the Writing Desk's voice client.

    ``factory(tier)`` returns the client the voice node speaks through for a
    run whose evidence carries *tier* content, or ``None`` to keep the desk's
    deterministic rendering. The production factory resolves through
    :class:`creek.classify.llm.router.ModelRouter`, so Intimate content is
    redirected to a local provider by the chokepoint (#658/#661); this module
    never picks a provider itself.

    The sibling of :class:`creek_mcp.tools.draft.DraftLLMFactory` and
    :class:`creek_mcp.tools.compile.CompileLLMFactory`, and added for the same
    reason (#1254): with no seam, ``creek.author`` answered ``status: ok``
    carrying a deterministically-stubbed body that no hermetic test could tell
    apart from a live one — the #460/#649/#658 signature.

    Invoked lazily, inside the run, so an unconfigured provider fails only an
    actual authoring call and never server startup.
    """

    def __call__(self, tier: PrivacyTier | None) -> AuthorLLMClient | None: ...


def _error_response(
    reason: str,
    *,
    tier_ceiling: TierCeiling,
    dry_run: bool,
) -> dict[str, Any]:
    """Render a structured error envelope (used on every failure path)."""
    return {
        "status": "error",
        "tool": TOOL_NAME,
        "tier_ceiling": tier_ceiling.value,
        "tier_ceiling_enforced": False,
        "dry_run": dry_run,
        "reason": reason,
    }


def _privacy_refusal_response(
    draft: AuthoredDraft,
    leaks: list[ReflectionFinding],
    *,
    tier_ceiling: TierCeiling,
) -> dict[str, Any]:
    """Render the refusal envelope for a draft the privacy gate condemned.

    Built on :func:`creek_mcp.tier_ceiling.refusal_response` so this is the same
    ``status: "refused"`` shape ``creek.link``, ``creek.report``,
    ``creek.state.read``, ``creek.journal`` and ``creek.upload`` already emit,
    rather than a sixth hand-rolled refusal: every consumer's ``status != "ok"``
    branch then handles it for free. Not ``status: "error"`` — nothing
    malfunctioned. :func:`_error_response` owns the malformed/blew-up path, and
    collapsing a successful refusal into it would make a provider outage
    indistinguishable from a privacy stop.

    ``body``, ``claims`` and ``provenance`` are *absent*, not emptied. A
    falsy-but-present ``body`` is exactly the shape that lets a caller print
    nothing and believe it published a draft; with the key gone,
    ``response["body"]`` raises at the moment of the refusal instead.

    Every value is copied field by field on purpose. Never reach for
    ``draft.model_dump()`` here: :class:`~creek.author.models.AuthoredDraft`
    carries a ``rendered_text`` ``@computed_field`` that aliases ``body`` and
    *is* included in dumps, so any dump smuggles the condemned prose back onto
    the wire under a second name.

    Args:
        draft: The condemned draft. Only its non-content metadata is echoed.
        leaks: Its findings, already filtered to :data:`_PRIVACY_DIMENSION` by
            :func:`_draft_response` — see there for why the filter must happen
            before anything is serialised.
        tier_ceiling: The caller's ceiling, echoed back unchanged.

    Returns:
        The canonical refusal keys plus this verb's own non-content metadata and
        the privacy findings.
    """
    return refusal_response(
        tool=TOOL_NAME,
        ceiling=tier_ceiling,
        reason=_PRIVACY_REFUSAL_REASON.format(count=len(leaks)),
    ) | {
        # "refused" with "enforced: True" is not a contradiction. This flag is a
        # claim about the #660 retrieval filter, which ran and did its job: at
        # ceiling=all the caller *is* admitted to intimate content, so no
        # ceiling was violated. The breach is against a different gate — the
        # medium contract's `default_privacy_tier` (`open` in all six shipped
        # templates) — which is exactly why the leak survives at `all` and why
        # every ceiling=open probe missed it.
        "tier_ceiling_enforced": True,
        "dry_run": False,
        "medium": draft.medium,
        "query": draft.query,
        "verdict": draft.verdict,
        "rounds": draft.rounds,
        "privacy_findings": [
            {
                "dimension": finding.dimension,
                "severity": finding.severity,
                "message": finding.message,
            }
            for finding in leaks
        ],
    }


def _draft_response(
    draft: AuthoredDraft,
    *,
    tier_ceiling: TierCeiling,
) -> dict[str, Any]:
    """Render an :class:`AuthoredDraft` into the MCP success envelope.

    A draft carrying any :data:`_PRIVACY_DIMENSION` finding is refused instead
    (#1353). The desk's HARD privacy check fires when the drafted prose
    reproduces a cited fragment's protected text verbatim, and the success
    envelope would then carry that text three times over — in ``body``, in
    ``claims[*].claim`` and in ``provenance[*].claim_excerpt`` — under
    ``status: "ok"``.

    The trigger is the finding *dimension*, never ``verdict == "ESCALATE"``:
    escalation is routine here (an empty vault escalates, and any unresolved
    soft finding escalates once the round budget runs out —
    ``creek/author/conductor.py:430-436``), so gating on the verdict would
    withhold ordinary drafts. It is not conditioned on severity either:
    ``check_privacy_compliance`` only emits ``HIGH`` today, and a
    dimension-only trigger stays correct if a future ``MID`` privacy finding
    appears.

    The findings are filtered to that one dimension *here*, before anything
    reaches an envelope. Echoing all of ``draft.findings`` would undo the
    scrub, because two sibling checks interpolate corpus text into their own
    messages: ``biographical_grounding`` embeds a sentence lifted from the
    drafted body (``creek/author/checks.py:176``) and
    ``attribution_correctness`` embeds the borrowed claim's text — the cited
    fragment's title, on the deterministic path (``checks.py:536``).
    Hence the refusal key is ``privacy_findings`` and not ``findings`` — a name
    nobody can "complete" by widening the filter without also having to rename
    it.

    Args:
        draft: The desk's finished draft.
        tier_ceiling: The caller's ceiling, echoed back unchanged.

    Returns:
        The success envelope, or — when the draft carries a privacy finding —
        the refusal envelope from :func:`_privacy_refusal_response`.
    """
    leaks = [leak for leak in draft.findings if leak.dimension == _PRIVACY_DIMENSION]
    if leaks:
        return _privacy_refusal_response(draft, leaks, tier_ceiling=tier_ceiling)
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": tier_ceiling.value,
        # #660: the specialists tier-filter retrieval against the ceiling, so
        # the echo now reflects real enforcement.
        "tier_ceiling_enforced": True,
        "dry_run": False,
        "medium": draft.medium,
        "query": draft.query,
        "body": draft.body,
        "verdict": draft.verdict,
        "rounds": draft.rounds,
        # The voice call stopped at its output ceiling: the body ends mid-thought.
        "truncated": draft.truncated,
        "provenance": [entry.model_dump(mode="json") for entry in draft.provenance],
        "claims": [
            {"claim": entry.claim_excerpt, "source_fragments": entry.fragment_ids}
            for entry in draft.provenance
        ],
        # #1261: the same signal the CLI prints, carried on the wire so an MCP
        # consumer is not the one surface left guessing why a draft cites
        # nothing. A LIST rather than a bool: it extends without a contract
        # change if a second advisory is ever added, and no existing test
        # makes an exact-key claim on this envelope.
        "warnings": ([ZERO_EVIDENCE_WARNING] if has_zero_evidence(draft) else []),
    }


def author_tool(
    *,
    vault_path: Path,
    query: str,
    llm_factory: AuthorLLMFactory | None = None,
    medium: str = "research",
    max_rounds: int | None = None,
    dry_run: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    on_stage: StageCallback = ignore_stage,
    provenance: InferenceProvenance | None = None,
) -> dict[str, Any]:
    """Author *query* via the real Writing Desk and return the result.

    Mirrors the ``creek author`` CLI args. Delegates to ``run_author`` (or
    ``plan_author`` when ``dry_run`` is set); the verb adds no desk logic. The
    caller's privacy ceiling is recorded **and enforced** (#660): it is
    converted to a :class:`~creek.classify.privacy_filter.PrivacyTierOverride`
    and threaded to the specialists, which exclude above-ceiling fragments from
    the evidence.

    Args:
        vault_path: The vault to author from.
        query: The user query to author about.
        llm_factory: Tier-keyed builder for the desk's voice client (#1254).
            ``None`` — the value a caller that never speaks to a model wants —
            keeps the desk's deterministic rendering. The server bootstrap
            supplies :func:`creek_mcp.server._build_author_llm`; tests pass a
            stub, which is what makes the wire past the desk assertable.
        medium: Target medium (``research`` or ``chat``).
        max_rounds: Optional override for the voice/reflect round bound.
            Defaults to the vault's ``author.max_author_rounds`` config (3),
            mirroring the ``creek author --max-rounds`` help (#1465).
        dry_run: When set, return the plan + evidence summary, not a draft.
        privacy_tier_ceiling: Privacy gate (FEAT-010).
        consumer: Audit consumer identifier.
        on_stage: Sink told each :data:`AUTHOR_STAGES` boundary the desk
            crosses (#1966). Stage names only; defaults to a no-op. The dry
            run composes nothing and reports nothing.
        provenance: The call's route recorder (#1967), filled by the
            production voice factory when it resolves a provider. A success
            envelope echoes its label as ``inference_source``; a deterministic
            render, a dry run and a refusal carry none.

    Returns:
        On success, the draft envelope (body, verdict, provenance, cited
        ``claims``) or — for ``dry_run`` — the plan + evidence summary. A draft
        the desk's privacy check condemned yields ``status: refused`` instead,
        carrying the ``privacy_findings`` but none of the three content keys
        (#1353); see :func:`_draft_response`. An unsupported medium, or any
        desk failure, yields ``status: error`` with a ``reason``: the medium's
        own message, or the fixed reason :func:`_desk_failure_reason` picks —
        never a desk exception's text (#2082).
    """
    # #660: the ceiling is enforced — converted to a PrivacyTierOverride below
    # and threaded into the specialists, which exclude above-ceiling fragments
    # from the retrieved evidence.
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "query": query,
            "medium": medium,
            "dry_run": dry_run,
            "max_rounds": max_rounds,
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    try:
        require_supported_medium(medium)
    except ValueError as exc:
        return _error_response(
            str(exc), tier_ceiling=privacy_tier_ceiling, dry_run=dry_run
        )

    # Any desk failure (bad config, provider error, missing contract, a
    # downstream validation error, ...) must surface as a structured envelope,
    # never an unhandled exception across the MCP boundary. This mirrors the
    # broad boundary catch the other MCP write tools use (e.g. purge), but
    # answers a fixed reason rather than the exception's text (#2082).
    # #660: enforce the ceiling by converting it to the core privacy override
    # threaded into the specialists.
    override = to_privacy_override(privacy_tier_ceiling)
    try:
        if dry_run:
            plan = plan_author(
                medium=medium,
                query=query,
                vault=vault_path,
                override=override,
            )
            return {
                "status": "ok",
                "tool": TOOL_NAME,
                "tier_ceiling": privacy_tier_ceiling.value,
                "tier_ceiling_enforced": True,
                "dry_run": True,
                "medium": medium,
                "query": query,
                "plan": plan["plan"],
                "evidence": plan["evidence"],
            }
        # #658/#661: the injected factory is tier-keyed so live voicing of
        # Intimate content is redirected to a local provider by the chokepoint,
        # and may answer ``None`` (deterministic stub) for an unavailable
        # provider — so the tool never hard-fails on a missing backend.
        draft = run_author(
            medium=medium,
            query=query,
            vault=vault_path,
            max_rounds=max_rounds,
            voice_client_factory=llm_factory,
            override=override,
            on_stage=on_stage,
        )
    except Exception as exc:
        # Type name in the message; the text — a vendor message, a URL, a
        # server path — rides only the traceback, passed explicitly (#2082).
        logger.exception("author failed: %s", type(exc).__name__, exc_info=exc)
        return _error_response(
            _desk_failure_reason(exc),
            tier_ceiling=privacy_tier_ceiling,
            dry_run=dry_run,
        )
    response = _draft_response(draft, tier_ceiling=privacy_tier_ceiling)
    source = None if provenance is None else provenance.source
    if response["status"] == "ok" and source is not None:
        response[INFERENCE_SOURCE_FRONTMATTER_KEY] = source
    return response


def _desk_failure_reason(exc: Exception) -> str:
    """Return the ``reason`` a failed desk run answers (#2082).

    The sibling of :func:`creek_mcp.tools.draft._provider_reason`, re-stated
    rather than imported so no tool imports another (#1772).

    Args:
        exc: What ``run_author`` or ``plan_author`` raised.

    Returns:
        The router's own message for
        :class:`~creek.classify.llm.router.IntimateRoutingError` — the residual
        the module docstring documents, deliberately not widened —
        :data:`~creek.classify.llm.providers.REQUEST_KEY_REJECTED_REASON` for a
        writer's key its vendor refused, :data:`AUTHOR_PROVIDER_FAILED_REASON`
        for any other provider failure, and :data:`AUTHOR_DESK_FAILED_REASON`
        for everything else.
    """
    if isinstance(exc, IntimateRoutingError):
        return str(exc)
    if isinstance(exc, RequestKeyRejectedError):
        return REQUEST_KEY_REJECTED_REASON
    if isinstance(exc, PROVIDER_CALL_FAILURES):
        return AUTHOR_PROVIDER_FAILED_REASON
    return AUTHOR_DESK_FAILED_REASON


def save_authored_draft(vault_path: Path, response: dict[str, Any]) -> Path:
    """Write a successful ``creek.author`` envelope's draft into the vault.

    The synchronous verb hands its body straight back and saves nothing. A call
    that outlived the MCP deadline has nobody to hand it to (#1966), so the
    detached path files it beside ``creek draft``'s drafts, under the same
    naming rule (:func:`creek.generate.drafts.reserve_draft_path`), where the
    person and ``creek.state.read`` can reach it. Only ever called with an
    ``ok`` envelope: a draft the privacy gate condemned carries no body and is
    never written.

    Args:
        vault_path: The vault the draft was authored from.
        response: The ``ok`` envelope :func:`author_tool` returned.

    Returns:
        The absolute path of the written draft.
    """
    generated = datetime.now(tz=UTC)
    # Named for the desk and the medium, never the query: the path is the one
    # piece of this draft a job result carries, and it should carry no prose.
    target = reserve_draft_path(
        vault_path, f"{_AUTHORED_DRAFT_STRATEGY} {response['medium']}", generated
    )
    post = frontmatter.Post(
        content=str(response["body"]),
        type="draft",
        title=draft_title(str(response["body"]), str(response["medium"])),
        status="draft",
        idea_strategy=_AUTHORED_DRAFT_STRATEGY,
        medium=response["medium"],
        verdict=response["verdict"],
        rounds=response["rounds"],
        truncated=bool(response.get("truncated", False)),
        source_fragments=_cited_fragment_ids(response),
        generated_date=generated.isoformat(),
    )
    if INFERENCE_SOURCE_FRONTMATTER_KEY in response:
        post[INFERENCE_SOURCE_FRONTMATTER_KEY] = response[
            INFERENCE_SOURCE_FRONTMATTER_KEY
        ]
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


_H1: Final[re.Pattern[str]] = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def draft_title(body: str, medium: str) -> str:
    """Return a saved draft's ``title``: the essay's own first ``# heading``.

    It used to be the caller's query — which, from an agent, is a page of
    instructions ("You are writing a post for my blog IN MY OWN VOICE…"), so
    the vault listed every draft under its prompt. A body with no top-level
    heading falls back to the desk and medium, the same prose-free name the
    file path already carries.

    Args:
        body: The drafted markdown.
        medium: The medium the desk drafted for.

    Returns:
        The heading text, or ``"Writing Desk <medium>"``.
    """
    match = _H1.search(body)
    if match is not None and match.group(1).strip():
        return match.group(1).strip()
    return f"Writing Desk {medium}"


def _cited_fragment_ids(response: dict[str, Any]) -> list[str]:
    """Return the distinct fragment ids an ``ok`` envelope cites, in order."""
    cited: list[str] = []
    for claim in response["claims"]:
        for fragment_id in claim["source_fragments"]:
            if fragment_id not in cited:
                cited.append(fragment_id)
    return cited


def author_job_result(
    response: dict[str, Any], draft_path: str | None
) -> dict[str, Any] | None:
    """Project a ``creek.author`` response onto what a job record may keep.

    Status words and counts only: no ``body``, no ``query``, no ``claims`` and
    no ``provenance`` — a job result is served to whoever holds the handle,
    long after the call. Any status other than ``ok`` projects to ``None``,
    the job store's ``failed``, as on ``/v1`` (#1605).

    Args:
        response: The dict :func:`author_tool` returned.
        draft_path: The vault-relative path :func:`save_authored_draft` wrote,
            or ``None`` when the body went back synchronously instead.

    Returns:
        The content-free result, or ``None``.
    """
    if response.get("status") != "ok":
        return None
    result = {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": response["tier_ceiling"],
        "medium": response["medium"],
        "verdict": response["verdict"],
        "rounds": response["rounds"],
        "truncated": bool(response.get("truncated", False)),
        "draft_path": draft_path,
        "claim_count": len(response["claims"]),
        "source_count": len(_cited_fragment_ids(response)),
        "warnings": list(response["warnings"]),
    }
    if INFERENCE_SOURCE_FRONTMATTER_KEY in response:
        result[INFERENCE_SOURCE_FRONTMATTER_KEY] = response[
            INFERENCE_SOURCE_FRONTMATTER_KEY
        ]
    return result
