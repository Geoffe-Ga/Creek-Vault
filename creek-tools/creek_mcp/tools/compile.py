"""``creek.compile`` MCP tool — roll fragments into a compiled page.

Wraps :func:`creek.compile.engine.compile_to_vault` (FEAT-003).

Four guarantees this module enforces (#848 — before that fix the first
of them was asserted in this docstring and implemented nowhere):

- **The write-side ceiling is checked against the *source* tiers, before
  any state-affecting step.** Compile's variant of the FEAT-011 write-side
  rule gates the classified tiers of the fragments being rolled up
  rather than the tier of the page being created: a caller cannot
  compile a page out of fragments they *named* and could not read.
  (Only the named ids are checked — an admitted fragment's own
  ``structural_path`` ancestry is not, which is a tracked follow-up.)
  The check runs
  ahead of any LLM client being built, any page being written, and any
  paradox being logged — the three places the source ids would
  otherwise escape (enumerated in :func:`compile_tool`). And when that
  client *is* built, it is built **for the routing tier** — the more
  sensitive of the ceiling and the admitted sources — so the source
  titles the prompt carries reach a model only through
  :class:`creek.classify.llm.router.ModelRouter`'s
  ``Intimate``-never-cloud chokepoint (#928).
- **The refusal names nothing.** It carries the fixed
  :data:`_ABOVE_CEILING_REASON`: no ids, no titles, no tiers, not even a
  count of how many sources were above the ceiling.
- **The refusal is audited, while the argument-validation refusals are
  not.** An attempted ceiling violation is an operator-relevant signal;
  a typo'd ``target_kind`` is not. This mirrors ``save`` / ``journal``
  and is a deliberate departure from ``reflect``, which appends
  unconditionally at the top of the call — :func:`compile_tool`
  explains why compile cannot do the same.
- **The gate and the engine share one fragment loader.** Both read the
  vault through :func:`creek.vault.reader.iter_vault_fragments`, so the
  two can never disagree about which files exist or which of them are
  fragments (see :func:`_survey_sources`).

**Idempotency semantics (audit-dedup only).** The wrapper fingerprints
the target file after each compile and stamps the hash under
``00-Creek-Meta/audit/compile-<target_id>.hash``. On a re-run whose
output matches the stored hash, the wrapper returns ``status="noop"``
**and skips the audit-log append** — but the underlying engine still
ran (LLM call + file write). This satisfies the FEAT-011 acceptance
test ("re-invoking ``creek.compile`` doesn't double-write audit
entries on no-op runs"); it deliberately does *not* attempt to skip
the LLM call itself, because that would require an input fingerprint
(source fragment IDs + bodies + prompt) the wrapper does not own.
Engine-side LLM-call dedup is tracked separately as a follow-up.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Protocol, cast

from creek.compile.engine import TARGET_KINDS, compile_to_vault
from creek_mcp.audit import MCPAuditLog
from creek_mcp.source_tiers import max_source_tier, source_tiers
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    routing_tier,
    write_tier_allowed,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.compile.engine import CompileLLM
    from creek.models import CompileTargetKind, PrivacyTier

TOOL_NAME = "creek.compile"

# The above-ceiling refusal reason is a fixed string naming no fragment id, no
# title, no tier, and no count. Echoing the offending ids would turn a single
# call into a bulk tier-classification oracle — submit 100 ids, learn 100 tiers
# in one round trip. A count would be nearly as useful to an attacker: it makes
# group testing *exact*, so a caller could binary-search a batch knowing
# precisely how many above-ceiling ids each half holds.
#
# ACCEPTED RESIDUAL RISK: a bare boolean is still a group-testing oracle — a
# caller who resubmits subsets of a batch identifies every above-ceiling id in
# O(k log n) calls. Two things that argument must NOT lean on:
#
# - *Id unguessability is not a control here.* Ids are deterministic
#   (``creek.ingest.base.generate_fragment_id`` hashes source+timestamp+content;
#   ``generate_child_fragment_id`` hashes ``f"{parent_id}:{level}:{index}"``), so
#   from a single admitted parent id a caller can enumerate every child id with
#   no knowledge of the content. The id space around known content IS sweepable;
#   only a blind sweep of the full 48-bit space is infeasible.
# - *Probe cost does NOT leak where the offending fragment sits.* The walk that
#   makes that true left this module with the code it describes (#958): it is
#   now ``creek_mcp.source_tiers.source_tiers``, whose docstring carries the
#   full analysis — the loader materialises the whole directory before
#   returning and the id filter runs to completion, so the cost of a probe is
#   uniform *by construction* rather than resting on an accident of iteration
#   order. The reasoning moved rather than being dropped because the property
#   is now shared: switching that one function to a lazy loader would re-open
#   the timing channel here and in ``creek.draft`` at the same time.
#
# Accepted because the real control is the audit trail, not id entropy: every
# True probe appends a ``creek.compile`` entry distinguishable from a success
# (no ``created_path``, no ``affected_fragment_ids``), so a sweep is detectable
# by rate. Noting the attacker's counter honestly — appending one known-missing
# id makes the negative half fall through to the engine's not-found refusal,
# which is NOT audited, so only positive probes are logged. Accepted also
# because *some* distinguishable refusal is required for a legitimate client's
# bug to be debuggable at all.
_ABOVE_CEILING_REASON: Final[str] = "source fragments exceed tier ceiling"


class CompileLLMFactory(Protocol):
    """Tier-keyed builder for the prompt → JSON-text LLM client.

    ``factory(tier)`` returns the client for a call carrying *tier*
    content. The production factory resolves through
    :class:`creek.classify.llm.router.ModelRouter`, so an ``INTIMATE``
    tier is forced onto the local model — or refused — rather than
    egressing (#928); this module never picks a provider itself, it only
    derives the routing tier (:func:`creek_mcp.tier_ceiling.routing_tier`)
    and hands it over.

    The factory is invoked lazily so an unconfigured LLM provider only
    fails the ``creek.compile`` invocation, not server startup — and not
    at all when the source-tier gate refuses. The server bootstrap
    supplies a production factory; tests pass a stub.
    """

    def __call__(self, tier: PrivacyTier) -> CompileLLM: ...


def _fingerprint(path: Path) -> str | None:
    """Return a content hash for *path* or ``None`` if it does not exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _SourceGate(NamedTuple):
    """The two signals one walk over the requested source fragments yields.

    Both are derived from the same
    :func:`creek_mcp.source_tiers.fragment_tier` reading of the same
    file, so admission and routing can never disagree about a fragment.

    Attributes:
        above_ceiling: Whether at least one requested fragment sits above
            the caller's ceiling, i.e. whether the call must be refused.
        max_tier: The most sensitive tier among the requested fragments
            that resolved — what the LLM call must be routed as.
    """

    above_ceiling: bool
    max_tier: PrivacyTier


def _survey_sources(
    vault_path: Path, fragment_ids: list[str], ceiling: TierCeiling
) -> _SourceGate:
    """Survey the requested source fragments: admission decision + routing tier.

    Deliberately *not* named as a predicate. It returns a
    :class:`_SourceGate`, not a bool, so ``if _survey_sources(...)`` would be
    vacuously true; the caller must branch on ``.above_ceiling`` explicitly.

    The vault is walked through
    :func:`creek.vault.reader.iter_vault_fragments` — the exact function
    :func:`creek.compile.engine._load_fragments_for_compile` calls — so the set
    of files this gate inspects and the set the engine would compile are
    identical *by construction*. A bespoke ``frontmatter.load`` walk would
    diverge: :func:`creek.vault.reader.try_load_fragment` rejects files whose
    ``type`` is not ``fragment`` and files that fail ``Fragment`` schema
    validation, both of which a raw scan happily reads. That divergence would
    create a class of file one side sees and the other does not — precisely the
    bug class this gate exists to prevent. A file the shared loader skips
    (unreadable, non-fragment, schema-invalid) is invisible to gate and engine
    alike, and so fails closed to the engine's "not found".

    That parity is by construction but not instantaneous: the gate walks the
    vault, returns, and the engine then walks it again. A concurrent writer that
    *raises* a fragment's tier inside that window (``creek classify``, the
    ``creek.classify`` MCP tool, a text editor) would be compiled under the
    pre-write tier. The window is not caller-controlled and closing it means
    handing the loaded fragments to the engine — an engine signature change,
    tracked as a follow-up along with eliminating the duplicated walk.

    Admission is decided by :func:`creek_mcp.tier_ceiling.write_tier_allowed`
    rather than the :func:`~creek_mcp.tier_ceiling.tier_allowed` it delegates
    to. The two are behaviourally identical today, but this is a *write* tool
    enforcing the FEAT-011 write-side rule, and keeping every write gate on one
    predicate (``save.py``, ``journal.py``, here) means ``grep
    write_tier_allowed`` enumerates them all — and any future divergence
    between the read-side and write-side rules lands compile on the correct
    side automatically. For the same reason there is deliberately no
    ``ceiling is TierCeiling.ALL`` fast path: a second route to the answer
    would silently desync if ``ALL``'s semantics ever changed.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        fragment_ids: The ids the caller asked to compile. An id with no
            matching fragment in the vault is **not** a violation — it falls
            through to the engine's ``ValueError("Fragment(s) not found in
            vault: ...")`` so a legitimate caller still learns the id does not
            resolve. One deployment-dependent exception: when *no* requested id
            resolves, ``max_tier`` fails closed to ``INTIMATE`` (below), and
            ``llm_factory(tier)`` is evaluated as a call *argument* to
            ``compile_to_vault`` — so it runs before the engine's not-found
            check. On a vault whose ``generation`` **and** ``default`` stages
            are both cloud, the router's ``IntimateRoutingError`` therefore
            wins over the not-found message. That is safe (it fails closed and
            names no id) and it is not caller-influenced — it depends on the
            operator's model config, not on the request — but the not-found
            refusal is not guaranteed under that one configuration.
        ceiling: The caller's declared ceiling.

    Returns:
        A :class:`_SourceGate`. Its ``above_ceiling`` is ``True`` when at least
        one requested fragment is above *ceiling*; its ``max_tier`` is the most
        sensitive tier among the requested fragments that resolved, failing
        closed to ``INTIMATE`` when *none* of them did — with no evidence about
        what the call would carry, the safe assumption is the worst one.

        The *refusal decision* is still deliberately a bare bool: the offending
        ids never leave this function; see :data:`_ABOVE_CEILING_REASON` for
        why. ``max_tier`` is returned alongside it solely to key the LLM router
        (#928) and **must never appear in any response, refusal reason, or
        audit payload** — echoing it would hand the caller exactly the per-call
        tier-classification oracle :data:`_ABOVE_CEILING_REASON` exists to
        prevent, and would be strictly worse than the bare bool: the bool says
        only "something you named is above your ceiling", while ``max_tier``
        names the rank. That invariant is the one new leak surface returning
        the pair creates, so it is stated here rather than left implied.
    """
    # Both signals come off one shared survey (#958) so ``creek.compile`` and
    # ``creek.draft`` can never read the same file's tier two different ways —
    # and the fail-closed reduction is shared for the same reason.
    tiers = source_tiers(vault_path, fragment_ids)
    return _SourceGate(
        above_ceiling=any(not write_tier_allowed(tier, ceiling) for tier in tiers),
        max_tier=max_source_tier(tiers),
    )


def compile_tool(
    *,
    vault_path: Path,
    fragment_ids: list[str],
    target_kind: str,
    target_id: str,
    target_title: str,
    llm_factory: CompileLLMFactory,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Compile *fragment_ids* into a compiled-layer page and audit the run.

    A re-run whose output matches the last hash returns ``status="noop"``
    and the wrapper skips the audit-log append. The engine still runs
    every call; see the module docstring for the idempotency scope.

    Args:
        llm_factory: Tier-keyed builder for the compile LLM client;
            ``llm_factory(tier)`` returns it. Invoked lazily so the LLM
            provider only matters when this tool is actually called — and
            not at all when the source-tier gate refuses. The tier is the
            more sensitive of *privacy_tier_ceiling* and the sources'
            own classifications, so the production factory's router
            forces an intimate compile onto the local model (#928).
        privacy_tier_ceiling: The caller's ceiling. Compile's variant of
            the FEAT-011 write-side rule gates the *source* fragments'
            classified tiers, not the tier of the content created (which
            is what ``save`` gates): rolling an ``intimate`` fragment
            into a compiled page under ``ceiling=open`` is refused,
            because the page, its provenance, and the compile prompt
            would each carry content the caller cannot read.

    Returns:
        ``{status, tool, tier_ceiling, ...}`` — ``ok`` with the written
        ``compiled_path``, ``noop`` when a re-run reproduced the stored
        hash, or a structured ``refused`` response for one of: an
        unknown *target_kind*; an empty *fragment_ids*; a source
        fragment above *privacy_tier_ceiling*
        (:data:`_ABOVE_CEILING_REASON`, #848 — the only refusal that is
        audited); or an engine ``ValueError`` / ``RuntimeError``, such
        as a fragment id that does not resolve in the vault, an
        unavailable provider, or the router's
        :class:`~creek.classify.llm.router.IntimateRoutingError` when
        intimate sources have no local backend to fall back to.
    """
    if target_kind not in TARGET_KINDS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unknown target_kind {target_kind!r}; "
                f"supported: {', '.join(TARGET_KINDS)}"
            ),
        )
    if not fragment_ids:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason="fragment_ids must be non-empty",
        )

    # The write-side source-tier gate (#848). Its position is load-bearing in
    # both directions.
    #
    # It sits *below* the two argument-validation refusals above because
    # neither ordering leaks — those checks read only caller-supplied input and
    # touch no vault state, so they can answer nothing about the corpus. The
    # tiebreaker is cost asymmetry: this gate is a full ``01-Fragments`` walk,
    # so gating first would let any caller force a 35k-file parse with a
    # syntactically invalid call.
    #
    # It sits *above* everything that follows, because each of those steps
    # hands the source fragments somewhere the caller is not admitted to:
    #
    # - ``llm_factory(tier)`` is evaluated as an *argument* to
    #   ``compile_to_vault`` below, and the prompt the engine builds emits
    #   ``id:`` and ``title:`` for every source unconditionally
    #   (``_fragment_excerpt_for_prompt`` redacts only the *body*). That
    #   provider is now tier-routed — the factory is keyed with the routing
    #   tier, so an admitted intimate source is forced onto the local model
    #   or refused (#928). Routing is not admission, though: sending a
    #   fragment the caller may not read to a *local* model still hands it
    #   to a summariser they were never admitted to, and the compiled page
    #   that summary lands in is the leak. So the gate must still win.
    # - ``compile_to_vault`` writes the compiled page — carrying per-claim
    #   provenance that names the source ids — into ``02-Threads`` /
    #   ``03-Eddies`` / ``06-Frequencies``, pages any ``open``-ceiling read
    #   tool then serves: intimate ids laundered into open-tier artifacts.
    # - ``_append_paradox_log`` writes LLM-authored text plus the source ids to
    #   ``00-Creek-Meta/Processing-Log/paradoxes-during-compile.jsonl``.
    # - the hash marker further below: a refused call must leave zero
    #   idempotency state behind.
    #
    # The gate also deliberately wins over the engine's missing-id
    # ``ValueError``. Were it the other way round, a caller could pair one
    # above-ceiling id with a batch of guesses and read back from the
    # "not found" list which of those guesses exist.
    #
    # This refusal is audited while the two argument-validation refusals above
    # are not, mirroring ``save_tool`` / ``journal_ingest_tool``: an attempted
    # ceiling violation is an operator-relevant signal, a malformed call is
    # not. That is a deliberate departure from ``reflect_tool``, which appends
    # unconditionally at the top of the function. Appending unconditionally
    # here would append on *every* compile including no-ops, breaking
    # ``test_compile_writes_audit_then_noop_on_re_run`` — the FEAT-011
    # acceptance test asserting exactly one ``creek.compile`` entry across two
    # calls. Compile's audit contract is "one entry per materially-new page",
    # not "one entry per call".
    #
    # The audited args carry no source content: ``summarise_args`` collapses
    # ``fragment_ids`` to ``{"count": N}``, and ``affected_fragment_ids`` — the
    # one field the log persists verbatim — is omitted, as are ``created_path``
    # and ``created_tier`` (nothing was created). ``target_title`` is omitted
    # too: caller free text, irrelevant to a refusal.
    gate = _survey_sources(vault_path, fragment_ids, privacy_tier_ceiling)
    if gate.above_ceiling:
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args={
                "target_kind": target_kind,
                "target_id": target_id,
                "fragment_ids": list(fragment_ids),
            },
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal_response(
            tool=TOOL_NAME, ceiling=privacy_tier_ceiling, reason=_ABOVE_CEILING_REASON
        )

    kind = cast("CompileTargetKind", target_kind)
    # The more sensitive of the ceiling and the admitted sources, so no
    # declaration a caller can make buys cloud routing for intimate content:
    # a low ceiling loses to the sources' own tiers, and low-tier sources lose
    # to a high ceiling. Built here rather than by the factory because only the
    # wrapper has seen both signals — and only *after* the gate above, so a
    # refused call never reaches a provider at all.
    tier = routing_tier(privacy_tier_ceiling, gate.max_tier)
    try:
        written = compile_to_vault(
            fragment_ids=list(fragment_ids),
            vault_path=vault_path,
            target_kind=kind,
            target_id=target_id,
            target_title=target_title,
            llm=llm_factory(tier),
        )
    except (ValueError, RuntimeError) as exc:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )

    relative = str(written.relative_to(vault_path))
    post_hash = _fingerprint(written)
    pre_hash_marker_path = (
        vault_path / "00-Creek-Meta" / "audit" / f"compile-{target_id}.hash"
    )
    pre_hash = (
        pre_hash_marker_path.read_text(encoding="utf-8")
        if pre_hash_marker_path.exists()
        else None
    )
    if pre_hash is not None and pre_hash == post_hash:
        return {
            "status": "noop",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "compiled_path": relative,
            "affected_fragment_ids": list(fragment_ids),
        }

    pre_hash_marker_path.parent.mkdir(parents=True, exist_ok=True)
    if post_hash is not None:
        pre_hash_marker_path.write_text(post_hash, encoding="utf-8")

    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={
            "target_kind": target_kind,
            "target_id": target_id,
            "target_title": target_title,
            "fragment_ids": list(fragment_ids),
        },
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=relative,
        created_tier=None,
        affected_fragment_ids=list(fragment_ids),
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "compiled_path": relative,
        "target_kind": target_kind,
        "target_id": target_id,
        "affected_fragment_ids": list(fragment_ids),
    }
