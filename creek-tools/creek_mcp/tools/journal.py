"""``creek.journal`` MCP tool — Adepthood journal entry → fragment (#754).

Lets an Adepthood journal entry (raw content + timestamp + tier + a STABLE
external id) flow into the vault as a fragment so it is classified, linked, and
available to ``reflect``/``wheel`` — reusing the existing ledger-backed markdown
ingest rather than a parallel pipeline.

Idempotency + edit-in-place come for free from the shared
:func:`creek.ingest.pipeline.run_ingest`: the entry is staged as a markdown file
at a **stable path derived from the external id** (under a ``…/journal/…`` dir so
it is classified as :class:`~creek.models.SourcePlatform.JOURNAL`), so the source
ledger keys on that stable path. Re-sending the same entry is a no-op; an edited
entry with the same external id rewrites the fragment in place (preserving its id
and classifications), never orphaning a duplicate.

Tier is honored end-to-end: the tier is written into the staged entry's
frontmatter (which the markdown ingestor carries onto the fragment), and the
write-side ceiling refuses an entry whose tier exceeds the caller's ceiling — so
an INTIMATE entry is stored intimate and never admitted under a lower ceiling.

**The update-in-place path is gated too, on the tier of what it would destroy
(#970): you may only overwrite what you could have read.** The write-side
``write_tier_allowed`` check ranks the *incoming* entry and says nothing about
the fragment an existing ``external_id`` already maps to, so a caller at
``ceiling=open`` used to replace the body of a fragment persisted at
``privacy_tier: intimate``. The persisted tier survived (the escalate-only
privacy merge held) but the intimate body did not. Contrast ``creek.purge.*``,
which requires ``CREEK_MCP_ELEVATED_TOKEN`` for exactly this kind of
destruction; this path requires only the caller's own (lower) ceiling.
:func:`_refuse_unadmitted_overwrite` closes that, and the question it asks —
"could this caller have *read* the thing it is about to destroy?" — is a read
question, which is why it goes through
:func:`creek_mcp.read_gate.refuse_above_ceiling` (and so ``tier_allowed``)
rather than through ``write_tier_allowed``.

Gate ordering, all of it load-bearing:

1. malformed calls (blank ``content``/``external_id``, unknown ``tier``) refuse
   without an audit entry — there is no meaningful tier to record yet;
2. ``write_tier_allowed`` on the *incoming* tier — audited, then refused;
3. the overwrite gate on the tier of the *resolved existing* fragment —
   audited, then refused;
4. **only then** :func:`_stage_entry`, which before #970 ran first. Moving it
   below the gate is the half of this fix that is easy to miss: the staged
   copy under ``00-Creek-Meta/adepthood/journal/`` has no escalate-only
   ratchet, so a gate added *after* the staging write returns a correct
   refusal over an already-destroyed staged entry — body replaced and
   ``privacy_tier`` rewritten *downward*. It is the worse loss of the two,
   since the fragment at least keeps its tier through the privacy merge.

Two fail-closed rules, deliberately distinct — collapsing them breaks one:

* **no ledger record at all** → ``content_tier=None``, which the primitive
  admits by contract. This is creation, and it must keep working at every
  ceiling; failing closed here would make the ordinary Adepthood write path
  unusable below the tier of whatever the vault happens to hold.
* **a ledger record whose fragment does not resolve** (purged, deleted out of
  band, schema-invalid, or tombed into ``10-Liminal/Orphaned`` where the
  ``01-Fragments`` walk cannot see it) →
  :func:`creek.classify.privacy_filter.max_source_tier` reduces the empty tier
  set to ``INTIMATE``, so the overwrite is refused below ``ceiling=intimate``.
  Every divergence between this gate's locator and the writer's own id index
  therefore fails in the safe direction. Note ``PurgeEngine.purge_fragment``
  leaves a dangling ledger record (#1080), so this branch is reachable in
  normal operation; recovery is one broader call, not a ledger hand-edit —
  true only for a **local stdio** caller, per the consequence below.

Consequence to know: that recovery does not exist for a remote consumer.
``_BoundedFastMCP.call_tool`` caps every remote request at
``ceiling=personal`` — on :data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS`,
which has owned that rule for every transport since #1073 (``docs/mcp.md``
§"INTIMATE is never reachable remotely") — so a remote caller can never even
*request* ``ceiling=intimate``/``all``. Because ``creek classify``'s privacy
pass is escalate-only, once a journal fragment reaches ``intimate`` a remote
consumer — and Adepthood, the primary journal producer, IS a remote
consumer — can never send that ``external_id`` again: not to edit it, and
not even as an unchanged idempotent re-sync, because this gate sits above
the ``record.content_hash != new_hash`` comparison in
:func:`creek.ingest.pipeline.write_fragment_idempotent`. This is the
correct, intended security behaviour, not a bug; #1082 tracks a possible
content-hash carve-out for the unchanged-resend case specifically — left
for a later design pass, not implemented here.

ACCEPTED RESIDUAL RISK: the refusal is an existence-*and-rank* oracle — "an
above-ceiling fragment exists at this external_id" — matching the honesty
standard :mod:`creek_mcp.tools.reflect`'s own ACCEPTED RESIDUAL RISK block
sets for its analogous oracle. It is no *stronger* than the pre-existing
``action: created|updated`` bit on the existence question those two bits
share over the same caller-owned id namespace; the *rank* bit — "the
fragment at this id is above your ceiling" — is new, and is the price of
refusing at all rather than silently corrupting. It is also deliberately
**blurred**: every fail-closed unresolvable case (purged, tombed into
``10-Liminal/Orphaned``, schema-invalid, deleted out of band) collapses
into the identical refusal as a genuine above-ceiling fragment, so
``refused`` does not read as a clean tier — it means "above your ceiling
OR unresolvable". Option (b) from
#970 — making the refusal indistinguishable from a write failure — was
considered and rejected on the merits: it would have to answer either
``"ingest failed: …"`` (a lie the audit trail then carries) or ``status="ok"``
(worse — the caller believes the entry is stored, stops retrying, and silently
loses its own data). It does not even work, because the refusal returns before
staging, before a full ``MarkdownIngestor`` run and before a fragment write, so
the latency gap is enormous and ``reflect``'s timing-equalisation caveat would
apply with far more force than it does there.

The fix is **preventive only**: a body already clobbered by the pre-#970
behaviour survives nowhere (provenance records hashes and paths, not content).
Operator recovery for an already-clobbered vault is documented in
``docs/mcp.md`` — re-send the original entry at ``ceiling=intimate``/``all``
from a **local stdio** caller (Adepthood cannot do this step itself: it is a
remote consumer capped at ``ceiling=personal``, per the consequence above),
or restore from a vault-level backup. A pre-image backup is deliberately NOT
taken: it would mint a second plaintext copy of intimate content, i.e. a new
leak surface, to guard against a write this gate now refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

from creek.classify.privacy_filter import max_source_tier, source_tiers
from creek.ingest.journal_staging import JOURNAL_STAGING_RELDIR
from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.pipeline import derive_source_key, ledger_for_source, run_ingest
from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.read_gate import refuse_above_ceiling
from creek_mcp.staged_names import safe_stem
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, write_tier_allowed

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.ingest.pipeline import IngestRunResult

    # The ingest runner seam — production is ``run_ingest``; tests inject a stub
    # to exercise the failure path.
    _Runner = Callable[..., IngestRunResult]

TOOL_NAME = "creek.journal"
_SOURCE_TYPE = "markdown"
# A stable per-vault home for staged Adepthood entries — the shared constant
# keeps this tool and the purge engine's RTBF staged-entry sweep pointed at
# the same directory (#845). Aliased so the rest of the module reads unchanged.
_JOURNAL_INBOX = JOURNAL_STAGING_RELDIR
# Where JOURNAL-platform fragments are routed by the writer — recorded as the
# audit ``created_path`` (mirrors ingest_tool's dir-level created_path).
_FRAGMENT_ROUTING_DIR = Path("01-Fragments/Journal")
# The staged-name derivation now lives in creek_mcp.staged_names so this tool
# and ``creek.upload`` cannot compute two different stems for one external id
# (#1023). Aliased so the rest of the module reads unchanged.
_safe_stem = safe_stem


def _staged_path(vault_path: Path, external_id: str) -> Path:
    """Return the stable staged markdown path *external_id* maps to.

    Pure — it computes, and never creates. Split out of :func:`_stage_entry`
    (#970) because the overwrite gate has to derive the ledger source key
    *before* deciding whether the caller may write anything at all, and a
    "compute the path" helper that also writes the file would make that
    ordering impossible to express.
    """
    return vault_path / _JOURNAL_INBOX / f"{_safe_stem(external_id)}.md"


def _stage_entry(
    vault_path: Path, external_id: str, content: str, timestamp: str, tier: PrivacyTier
) -> Path:
    """Write the entry to its stable staged markdown path and return it."""
    staged = _staged_path(vault_path, external_id)
    staged.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content,
        privacy_tier=tier.value,
        date=timestamp,
        source_id=external_id,
    )
    staged.write_text(frontmatter.dumps(post), encoding="utf-8")
    return staged


def _resolve_fragment_id(vault_path: Path, staged: Path) -> str | None:
    """Return the fragment id the ledger mapped this staged entry to."""
    ledger = ledger_for_source(_SOURCE_TYPE, vault_path)
    if ledger is None:
        return None
    record = ledger.get(derive_source_key(str(staged), vault_path))
    return record.fragment_id if record is not None else None


def _existing_tier(vault_path: Path, fragment_id: str) -> PrivacyTier:
    """Return the CURRENT vault tier of *fragment_id*, failing closed.

    Read out of the vault rather than off the staged entry's frontmatter or
    the caller's own ``tier`` argument: both of those are stale the moment
    ``creek classify`` escalates the fragment, and a caller-supplied tier is
    not a gate at all. The shared
    :func:`creek.classify.privacy_filter.source_tiers` walk is used so this
    gate inspects exactly the files the rest of the pipeline treats as
    fragments; :func:`~creek.classify.privacy_filter.max_source_tier` then
    reduces, returning ``INTIMATE`` for an id that resolves to nothing. With
    no evidence about what the id holds, the safe assumption is the worst one.

    The shared walk also costs the same whatever it finds — it materialises
    the whole directory before filtering and never short-circuits at the match
    — so the refusal cannot leak *where* the protected fragment sits through
    timing. That property is documented on ``source_tiers`` itself and is a
    reason not to swap in a bespoke lazy scan for speed.

    Args:
        vault_path: Vault root.
        fragment_id: The id the source ledger resolved for this entry.

    Returns:
        The fragment's current tier, or ``INTIMATE`` when the id resolves to
        no readable fragment.
    """
    return max_source_tier(source_tiers(vault_path, [fragment_id]))


def _refuse_unadmitted_overwrite(
    vault_path: Path, external_id: str, ceiling: TierCeiling
) -> dict[str, Any] | None:
    """Refuse an update that would destroy content *ceiling* cannot read (#970).

    This is a write gate legitimately adopting a *read* primitive. The target
    is caller-addressed and singular — one ``external_id`` resolves to exactly
    one fragment — which is the property
    :mod:`creek_mcp.read_gate` says decides between refusing and excluding:
    there is nothing to partially admit, because you cannot overwrite half a
    body. And the question being asked really is a read question ("could this
    caller have read what it is about to overwrite?"), so admission is decided
    by ``tier_allowed`` via the primitive rather than by ``write_tier_allowed``.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.
        ceiling: The caller's admission ceiling.

    Returns:
        ``None`` when the overwrite is admitted — including when the id maps
        to no ledger record at all, which is a *creation* and must keep
        working at every ceiling. Otherwise the canonical four-key refusal,
        which names neither the resolved fragment nor its tier.
    """
    staged = _staged_path(vault_path, external_id)
    existing_id = _resolve_fragment_id(vault_path, staged)
    existing = None if existing_id is None else _existing_tier(vault_path, existing_id)
    return refuse_above_ceiling(
        tool=TOOL_NAME,
        content_tier=existing,
        ceiling=ceiling,
    )


def _validated_entry_tier(
    *, content: str, external_id: str, tier: str, ceiling: TierCeiling
) -> PrivacyTier | dict[str, Any]:
    """Return the parsed entry tier, or the refusal a malformed call earns.

    Malformed calls refuse **without** an audit entry, mirroring
    ``save_tool``/``ingest_tool``: there is no meaningful tier to record yet.
    Both admission gates and the success path do audit, since the audit trail
    is how a privacy-tier violation would be investigated.

    Args:
        content: The journal entry body.
        external_id: The caller's idempotency key.
        tier: The caller's declared tier, as the raw string it arrived as.
        ceiling: The caller's admission ceiling, echoed in a refusal.

    Returns:
        The parsed :class:`~creek.models.PrivacyTier`, or a structured refusal
        naming which malformation was found. The two reasons stay distinct
        and specific: neither is derived from vault content, so neither is an
        oracle, and a client whose call is simply wrong deserves to be told
        which way it is wrong.
    """
    if not content.strip() or not external_id.strip():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            reason="content and external_id are required",
        )
    try:
        return PrivacyTier(tier)
    except ValueError:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            reason=f"unknown tier {tier!r}",
        )


def _action_of(result: IngestRunResult) -> str:
    """Collapse the single-entry run tally into one action word."""
    if result.created:
        return "created"
    if result.updated:
        return "updated"
    return "unchanged"


def journal_ingest_tool(
    *,
    vault_path: Path,
    content: str,
    external_id: str,
    timestamp: str | None = None,
    tier: str = "open",
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    run: _Runner | None = None,
) -> dict[str, Any]:
    """Ingest one Adepthood journal entry as a vault fragment (idempotently).

    Args:
        vault_path: Vault root.
        content: The journal entry body.
        external_id: The Adepthood-side stable id — the idempotency key. The same
            id updates in place; a new id creates a new fragment.
        timestamp: ISO-8601 entry time; defaults to now (UTC) when absent.
        tier: The entry's privacy tier (``open``/``personal``/``intimate``).
        privacy_tier_ceiling: The caller's admission ceiling — an entry whose
            tier exceeds it is refused, not downgraded, and so is an update
            that would overwrite a fragment the ceiling could not read.
        consumer: Free-form consumer id for the audit log.
        run: Ingest-runner seam; production passes ``None`` and gets
            :func:`creek.ingest.pipeline.run_ingest`.

    Returns:
        ``{status, tool, tier_ceiling, external_id, fragment_id, action, tier,
        warnings}`` on success (``action`` ∈
        ``created``/``updated``/``unchanged``; ``warnings`` is the run's
        content-free advisory channel, an empty list when the run was quiet),
        or a structured refusal — the canonical four-key one carrying
        :data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON`, naming
        neither the protected fragment nor its tier, when the overwrite gate
        fires (#970).
    """
    entry_tier = _validated_entry_tier(
        content=content,
        external_id=external_id,
        tier=tier,
        ceiling=privacy_tier_ceiling,
    )
    # A non-tier answer is the refusal a malformed call earned; from here down
    # ``entry_tier`` is narrowed to the parsed tier.
    if not isinstance(entry_tier, PrivacyTier):
        return entry_tier

    audit_args = {"external_id": external_id[:64], "tier": tier}
    # Gate 1 — the INCOMING tier. Deliberately kept on write_tier_allowed
    # rather than folded into the read primitive below, so ``grep
    # write_tier_allowed`` still enumerates every write-side create gate on
    # the surface (compile's argument).
    if not write_tier_allowed(entry_tier, privacy_tier_ceiling):
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"entry tier {entry_tier.value} exceeds the ceiling",
        )

    # Gate 2 — the tier of what an update would DESTROY (#970). It sits above
    # the staging write below it, so a refusal leaves the staged entry intact
    # as well as the fragment; _stage_entry used to run before all of this.
    # The refusal audit carries the caller's own arguments and nothing else:
    # no created_tier, no affected_fragment_ids, no created_path. The resolved
    # fragment id and the protected tier must not enter the trail, which is
    # served onward through other surfaces — read_gate's rule 1 is "never the
    # probed target id and never the outcome", and that binds these bytes
    # exactly as it binds the response.
    if (
        refusal := _refuse_unadmitted_overwrite(
            vault_path, external_id, privacy_tier_ceiling
        )
    ) is not None:
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal

    staged = _stage_entry(
        vault_path,
        external_id,
        content,
        timestamp or datetime.now(UTC).isoformat(),
        entry_tier,
    )
    runner = run if run is not None else run_ingest
    try:
        result = runner(
            ingestor_cls=MarkdownIngestor,
            source_type=_SOURCE_TYPE,
            input_path=staged,
            vault_path=vault_path,
        )
    except FileNotFoundError:
        return refusal_response(
            tool=TOOL_NAME, ceiling=privacy_tier_ceiling, reason="vault unavailable"
        )
    if result.errors:
        # Content was staged and tier-allowed but the write failed — audit the
        # attempt (with its tier) so the failure leaves a trace, like ingest_tool.
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
            created_tier=entry_tier.value,
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"ingest failed: {result.errors[0]}",
        )

    fragment_id = _resolve_fragment_id(vault_path, staged)
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args=audit_args,
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=str(_FRAGMENT_ROUTING_DIR),
        created_tier=entry_tier.value,
        affected_fragment_ids=[fragment_id] if fragment_id else [],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "external_id": external_id,
        "fragment_id": fragment_id,
        "action": _action_of(result),
        "tier": entry_tier.value,
        # ``ceiling_safe_warnings``, never ``warnings``: it is the only ingest
        # advisory channel that may cross this boundary, because the operator
        # channel interpolates real vault fragment ids this caller's ceiling
        # may not admit. See the ``warn`` doctrine in creek/ingest/pipeline.py
        # for why that call is made at the producer (#1372).
        "warnings": list(result.ceiling_safe_warnings),
    }
