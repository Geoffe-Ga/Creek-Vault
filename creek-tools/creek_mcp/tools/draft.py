"""``creek.draft`` MCP tool — generate an essay draft from a mined idea.

The wrapper accepts an injectable, **tier-keyed** ``llm_factory`` so
tests can exercise it without hitting a real model. In production the
server bootstrap injects a factory that resolves the provider through
:class:`creek.classify.llm.router.ModelRouter`, so an intimate draft is
forced onto the local model — or refused — instead of egressing (#958).
This module never picks a provider itself; it derives the routing tier
and hands it over.

Three properties of that routing rule, none of them visible at the call
site, all of them load-bearing:

- **The tier is derived from the seed's *source fragments* only.** The
  ``## Threads`` and ``## Eddies`` prompt sections render compiled-page
  bodies and frontmatter descriptions with **no tier filtering at all**
  (``creek.generate.drafts.DraftGenerator._compose_thread_section`` /
  ``._compose_eddy_section``, fed by
  :func:`creek.generate.drafts._load_threads_by_id` /
  :func:`creek.generate.drafts._load_eddies_by_id`), so whatever reaches
  the model through those sections does **not** raise the routing tier.
  That is a pre-existing residual in the #931 family, deliberately out
  of scope for #958, and stated here rather than left for the next
  reader to rediscover.
- **The ceiling alone is not sufficient** — which is the whole reason
  the sources are surveyed at all.
  :func:`creek.generate.drafts._render_fragment_section` emits
  ``### {fid}: {fragment.title}`` unconditionally, and under an ``open``
  ceiling :func:`creek.classify.privacy_filter.filter_fragments_by_tier`
  replaces a *personal* fragment's body with
  ``[Personal-tier summary: {title}]`` rather than dropping the fragment
  — so a personal fragment's id and title reach the prompt even though
  its body was redacted. Routing on ``routing_tier(ceiling, None)``
  would hand those to the cloud ``generation`` stage on the caller's
  say-so. ``intimate`` fragments, by contrast, are dropped outright at
  ceilings below intimate, so the personal case is the one that makes
  the survey necessary.
- **The derived tier never leaves this module.** It must not appear in
  any response, refusal reason, or audit payload: echoing it would hand
  the caller a per-call tier-classification oracle over the corpus — the
  same invariant :mod:`creek_mcp.tools.compile` documents in
  :func:`~creek_mcp.tools.compile._survey_sources`, where since #962 it
  holds structurally: that wrapper derives no tier at all, because the
  compile engine derives its own. The audit append records only the
  caller's own declared ceiling, and an ``ok`` response carries no tier
  at all.

  One narrow exception, stated rather than left implied because the
  claim above would otherwise be false: when the router refuses, the
  refusal reason is
  :class:`~creek.classify.llm.router.IntimateRoutingError`'s own message,
  which names the word "Intimate" and the configured cloud provider.
  That is a one-bit signal, and an *ambiguous* one — the fail-closed
  default routes a named-but-unresolved id as ``INTIMATE`` too, so the
  message cannot distinguish "a source really is intimate" from "an id
  did not resolve". It is also unreachable on any correctly-configured
  vault: it fires only when ``generation`` **and** ``default`` are both
  cloud, i.e. when no intimate content could be processed at all. This
  is the same residual ``creek.compile`` already accepts for the same
  error, and it is deliberately not widened here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from creek.classify.privacy_filter import max_source_tier, source_tiers
from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaMiner
from creek.models import Phase, PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    routing_tier,
    to_privacy_override,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.generate.mining import IdeaSeed

TOOL_NAME = "creek.draft"


class _DraftLLM(Protocol):
    """A prompt-completion callable returning the draft body."""

    # Positional-only (`/`) to match the canonical
    # :data:`creek.generate.drafts.DraftLLM` alias, which is a bare
    # ``Callable[[str], ...]`` and therefore names no parameter;
    # ``DraftGenerator`` likewise calls ``self._llm(prompt)`` positionally.
    # Without the `/` this Protocol would demand a keyword every real
    # implementor is free not to offer — do not drop it.
    def __call__(self, prompt: str, /) -> str:
        """Return the completion for *prompt*."""


class DraftLLMFactory(Protocol):
    """Tier-keyed builder for the prompt → draft-body LLM client.

    ``factory(tier)`` returns the client for a call carrying *tier*
    content. The production factory resolves through
    :class:`creek.classify.llm.router.ModelRouter`, so an ``INTIMATE``
    draft is forced onto the local model — or refused — rather than
    egressing (#958); this module never picks a provider itself, it only
    derives the routing tier (:func:`_source_routing_tier`) and hands it
    over.

    The factory is invoked lazily so an unconfigured LLM provider only
    fails an actual ``creek.draft`` invocation, not server startup — and
    not at all on the paths that return before a prompt exists (no idea
    seeds, out-of-range index). The server bootstrap supplies a
    production factory; tests pass a stub.
    """

    def __call__(self, tier: PrivacyTier) -> _DraftLLM: ...


def _source_routing_tier(
    vault_path: Path, idea: IdeaSeed, ceiling: TierCeiling
) -> PrivacyTier:
    """Return the tier this draft's LLM call must be keyed with (#958).

    Split out of :func:`draft_tool` to keep that function within the
    complexity budget, and because the empty-sources branch below needs
    more justification than a call site can carry.

    **An empty ``source_fragments`` is not the same as "nothing
    resolved".** The distinction has to be made *before* the survey runs,
    because :func:`creek.classify.privacy_filter.source_tiers` returns an empty
    list for both cases and the two must route differently:

    - ``source_fragments == ()`` means there is no vault content in this
      prompt at all. ``UNEXPLORED_ONTOLOGY`` is the only seed strategy
      that produces it (``creek.generate.mining._seed_from_ontology_tuple``),
      and it is also the only one with empty ``threads`` *and* empty
      ``eddies`` — its title and description are built purely from
      ontology enum labels. Failing closed to ``INTIMATE`` here would
      push every unexplored-ontology draft onto the local model for no
      privacy gain whatsoever, so the ceiling alone decides.
    - Sources were named but none resolved: fail closed to ``INTIMATE``.
      That default is not spelled out here — it belongs to the shared
      :func:`creek.classify.privacy_filter.max_source_tier` called below,
      which ``creek.compile.engine._routing_tier_for`` reduces with too,
      so the two tools cannot drift. With no evidence about what the call
      would carry, the safe assumption is the worst one.

    Reconciliation goes through the shared
    :func:`creek_mcp.tier_ceiling.routing_tier` rather than a bespoke
    ``max``: it takes the more sensitive of the ceiling-derived tier and
    the content tier, which is what makes the result uncheatable from
    outside. A caller can neither declare a low ceiling to win cloud
    routing for personal sources, nor pick low-tier sources to win it
    under a broad ceiling.

    Args:
        vault_path: Vault root; source fragments are read from
            ``01-Fragments`` by the shared survey.
        idea: The mined seed about to be drafted. Only its
            ``source_fragments`` are consulted — see the module docstring
            for why its ``threads`` / ``eddies`` do not raise the tier.
        ceiling: The caller's declared ceiling.

    Returns:
        The :class:`~creek.models.PrivacyTier` to key the LLM factory
        with. It is a routing signal only and **must never** be echoed
        back to the caller in any form.
    """
    if not idea.source_fragments:
        return routing_tier(ceiling, None)
    tiers = source_tiers(vault_path, idea.source_fragments)
    return routing_tier(ceiling, max_source_tier(tiers))


def draft_tool(
    *,
    vault_path: Path,
    llm_factory: DraftLLMFactory,
    skills_root: Path | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    phase: str = "unclassified",
    index: int = 0,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Mine ideas, draft the *index*-th, and persist with full provenance.

    The full draft body never enters the response so tier violations
    cannot leak via MCP — callers follow up with ``creek.state.read``
    or open the saved file directly to inspect the body.

    Args:
        llm_factory: Tier-keyed builder for the draft LLM client;
            ``llm_factory(tier)`` returns it. Invoked lazily, and only
            once the seed to be drafted is known, so neither the "no idea
            seeds" nor the out-of-range-index path spends a provider
            handshake (or a cloud credential) on a call that never
            drafts. The tier is the more sensitive of
            *privacy_tier_ceiling* and the seed's source fragments' own
            classifications, so the production factory's router forces an
            intimate draft onto the local model (#958).
        privacy_tier_ceiling: The caller's ceiling. It bounds which
            fragments the generator may render (via
            :func:`creek_mcp.tier_ceiling.to_privacy_override`) *and*
            contributes to the routing tier, but it is a floor rather
            than the whole answer — see the module docstring.

    Returns:
        ``{status, tool, tier_ceiling, ...}`` — ``ok`` with the saved
        ``draft_path``, ``empty`` when mining surfaced no seeds, or a
        structured ``refused`` response for an out-of-range *index*, an
        unavailable provider, the router's
        :class:`~creek.classify.llm.router.IntimateRoutingError` when
        intimate sources have no local backend to fall back to, or a
        generator ``RuntimeError``. No refusal reason names a fragment or
        its tier.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"phase": phase, "index": index},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    override = to_privacy_override(privacy_tier_ceiling)
    skills_dir = skills_root if skills_root is not None else vault_path / "creek-skills"
    seeds = IdeaMiner(privacy_override=override).mine_all(
        vault_path,
        current_phase=Phase(phase),
    )
    if not seeds:
        return {
            "status": "empty",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "reason": "no idea seeds surfaced",
        }
    if index < 0 or index >= len(seeds):
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"index {index} out of range (0..{len(seeds) - 1})",
        )
    idea = seeds[index]
    # Deriving the tier and building the client sit here — below both
    # non-drafting exits, above anything that touches a model — so a call that
    # produces no prompt never reaches a provider.
    tier = _source_routing_tier(vault_path, idea, privacy_tier_ceiling)
    try:
        llm = llm_factory(tier)
    except RuntimeError as exc:
        # ``IntimateRoutingError`` is a ``RuntimeError`` subclass, so this is
        # what turns "intimate content and no local backend configured" into a
        # structured refusal rather than an exception across the MCP boundary
        # (#958). A plainly unavailable provider takes the same path. Kept as
        # its own narrow ``try`` so ``generator`` below cannot be
        # possibly-unbound.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )
    generator = DraftGenerator(
        llm=llm,
        skills_root=skills_dir,
        privacy_override=override,
    )
    try:
        draft = generator.generate_draft(idea, vault_path=vault_path)
    except RuntimeError as exc:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )
    saved = generator.save_draft(draft, vault_path)
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "draft_path": str(saved.relative_to(vault_path)),
        "title": draft.title,
        "idea_strategy": draft.idea_strategy,
        "source_fragments": list(draft.source_fragments),
    }
