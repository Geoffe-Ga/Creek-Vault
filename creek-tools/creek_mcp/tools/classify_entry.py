"""``creek.classify.entry`` MCP tool — per-entry classification read (#874).

Hands back the ontology classification **one named fragment currently carries
on disk**: its frequency, its wavelength phase, its privacy tier, and how that
classification was last produced. It is a sibling of
:mod:`creek_mcp.tools.classify` rather than an addition to it, and the split is
the point: ``creek.classify`` is the whole-vault corpus-maintenance *pass* that
writes labels, this is a per-entry *read* that writes no vault content.
Folding the two into one tool would blur a pass that mutates every fragment
into a lookup that mutates none. (Since #2068 it does take the shared
content-mutation lock, so a call may create the zero-byte
``00-Creek-Meta/locks/content-mutations.lock`` — an advisory lock file that
holds no vault-derived bytes — beside the audit line it already appends.)

**It computes nothing, deliberately.** No ``RuleClassifier`` on demand, no LLM,
no persisted verdict. A per-fragment classify-and-write is a new mutation
surface that sits awkwardly beside the ratified "the pass is whole-vault,
idempotent, resumable, and takes no fragment selector" commitment, and it would
need its own escalate-only privacy argument, so it is held.

**Ingest does not classify, and that is the fact a consumer most needs.**
``creek.ingest``/``run_ingest`` touches exactly one ``creek.classify`` symbol —
``privacy_pass.escalate`` — and never the frequency/phase classifier. A
freshly written journal entry therefore reads ``frequency="unclassified"``,
``phase="unclassified"``, ``classification_method="none"`` until a pass runs,
and that is an honest answer rather than a failure. The remedy is the route
that shipped at contract ``0.10.0``: run ``creek.classify`` (or ``POST
/v1/classifications``), then read again. This is also why #874's original
inline-on-``creek.journal`` design was dropped — it would have returned those
constants forever, on every call, with every one of its tests passing.

``classification_method`` is what makes the answer legible. The stamp is
written *unconditionally* on any classify write, even when the verdict is
``unclassified``, so ``method="rules"`` with ``frequency="unclassified"`` means
*a pass ran and genuinely could not classify this*, while ``method="none"``
means *no pass has run*. The sentinel is ``"none"`` and not ``"unclassified"``
on purpose: that word is already a :class:`~creek.models.Frequency`,
:class:`~creek.models.Phase` **and** :class:`~creek.models.PrivacyTier` member,
and reusing it for a provenance field would collapse the exact distinction the
field exists to draw. The value is also **clamped** to the three published
methods rather than echoed: raw frontmatter is arbitrary user-controlled bytes,
and echoing it unclamped would put an unbounded content channel on the
Adepthood wire out of a file the caller may be admitted to only at rank level.

**Posture: GATED, and it REFUSES rather than excluding.** The template is
``creek.state.read`` (#969), not ``creek.reflect``: the rule
:mod:`creek_mcp.tools.state_read` states is that a target which is
caller-*addressed* and singular has nothing to partially admit, and a fragment
id is exactly that. So the gate is the shared
:func:`creek_mcp.read_gate.refuse_above_ceiling`, carrying
:data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON` — the
``state.read``/``journal``/``upload`` shape. Reflect's own above-ceiling reason
is deliberately *not* copied: :mod:`creek_mcp.read_gate` records why it was
never retrofitted, and adopting a tool-specific reason here would mint a
synonym for a refusal that must stay byte-identical across every above-ceiling
tier.

**One snapshot, inside the content lock (#2068).** The tier the gate compares
and the record the answer is built from come from **one** exhaustive
:func:`creek.vault.reader.iter_vault_fragments` pass, taken while
:func:`creek.vault.mutations.content_mutation_lock_path` is held — the lock
``creek classify`` and the ``creek.classify`` MCP tool rewrite tiers and
labels under. Before #2068 the gate read the tier through
:func:`creek.classify.privacy_filter.source_tiers` (one walk) and the record
came from a second walk, with no lock across either, so a re-tier landing
between them was served as the *old* tier paired with the *new*
classification — a response no single state of the vault ever held. The same
TOCTOU #930 closed for compile.

Each matching file's tier is read with
:func:`~creek.classify.privacy_filter.fragment_tier` — the per-file primitive
``source_tiers`` itself applies — and reduced by
:func:`~creek.classify.privacy_filter.max_source_tier`, so the gate keeps the
exact rules ``creek_mcp.tools.journal._existing_tier`` has. Three properties
come with it and none are re-derived here:

* it is the fragment's **current persisted** tier, never a caller-supplied one
  and never a staged-frontmatter one, both of which are stale the moment
  ``creek classify`` escalates the fragment;
* a **missing** ``privacy_tier`` key fails closed to ``INTIMATE``, distinctly
  from an *explicit* ``unclassified``. Reading the tier off the validated
  :class:`~creek.models.Fragment` instead would fail **open** there (#1033),
  which is why :func:`creek_mcp.read_gate.iter_admitted_fragments` is not the
  primitive for this tool;
* an id resolving to **nothing** reduces to ``INTIMATE`` as well, so every
  locator divergence fails safe — and an id carried by **several** files
  gates on the most sensitive of them, whichever one the answer is served
  from.

Note that an explicit ``privacy_tier: unclassified`` ranks with ``personal``
(#961), so it is **admitted at ``ceiling=personal``** and **refused at
``ceiling=open``**. That ranking is owned by
:data:`creek_mcp.tier_ceiling._TIER_RANK` and is not re-stated here.

ACCEPTED RESIDUAL RISK
----------------------
Two ways this tool is better than the nearest existing oracle, and one way it
is no better.

*Better, and by construction rather than by care.* The one walk never
short-circuits — ``iter_vault_fragments`` materialises the whole directory
before returning, a property ``source_tiers`` documents as load-bearing, and
the match list is built to completion before anything is decided — so one
exhaustive snapshot precedes the gate, a not-found id and a refused id cost
the same, and the refusal cannot leak *where* the protected fragment sits
through timing. Contrast ``creek_mcp.tools.reflect._resolve_entry``, which
walks a raw lazy ``rglob`` and returns at the match: a genuine timing channel.
Turning the snapshot into a lazy search that stops at the first match is an
easy, natural-looking edit that reintroduces exactly that channel. And the
refusal is the generic one, so it names neither the fragment nor its tier.

*The cost of the lock (#2068), accepted as #930 accepted it for compile.* A
call now creates the zero-byte lock file if it is absent (it holds no vault
content); it can wait up to
:data:`creek._fslock.DEFAULT_LOCK_TIMEOUT_SECONDS` behind a whole-vault
classify, link, compile, journal or upload that holds the lock; and on that
timeout it answers the content-free
:data:`~creek.vault.mutations.CONTENT_MUTATION_BUSY_REASON` refusal. That
refusal says only that *some* writer was busy, which every MCP write tool
already publishes, and it is decided before the snapshot is taken, so it
carries nothing about the addressed fragment.

*What is still published.* At ``ceiling=intimate`` or ``all``, ``"entry_ref not
found"`` and the generic refusal are distinguishable, which makes this a coarse
existence-and-rank oracle over fragment ids. That is the identical oracle
``creek.reflect`` has published since #751 and that ``creek.journal``'s #970
overwrite gate publishes over the same id namespace — a new instance, not a new
class. Below ``intimate`` it is not reachable at all, because the fail-closed
``INTIMATE`` reduction refuses an unresolvable id before anything is resolved.

*What it does not publish.* Strictly less than ``creek.reflect`` already does
over the same namespace: reflect returns model-generated prose grounded in the
fragment's *body*; this returns four bounded enum-valued strings and the
caller's own echoed input. Nothing becomes reachable that was not already
reachable at strictly broader granularity through ``creek.state.render``,
``creek.wheel`` and the compiled layer.
"""

from __future__ import annotations

from contextlib import ExitStack
from itertools import starmap
from typing import TYPE_CHECKING, Any, Final

from creek._fslock import VaultLockTimeoutError, vault_lock
from creek.classify.constants import (
    CLASSIFICATION_METHOD_KEY,
    LLM_METHOD,
    MANUAL_METHOD,
    RULES_METHOD,
)
from creek.classify.privacy_filter import fragment_tier, max_source_tier
from creek.vault.mutations import (
    CONTENT_MUTATION_BUSY_REASON,
    content_mutation_lock_path,
)
from creek.vault.reader import iter_vault_fragments
from creek_mcp.audit import MCPAuditLog
from creek_mcp.read_gate import refuse_above_ceiling
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment, PrivacyTier

TOOL_NAME: Final[str] = "creek.classify.entry"

_FRAGMENTS_SUBDIR: Final[str] = "01-Fragments"

NO_METHOD: Final[str] = "none"
"""``classification_method`` value meaning *no classify pass has run*.

Deliberately **not** ``"unclassified"``. That word is a ``Frequency``,
``Phase`` and ``PrivacyTier`` member, and overloading it here would leave a
consumer reading ``{frequency: "unclassified", classification_method:
"unclassified"}`` needing out-of-band knowledge that the second word means "no
pass ran" while the first means "a pass ran and declined" — which is precisely
the distinction this field exists to make legible without it.
"""

_PUBLISHED_METHODS: Final[frozenset[str]] = frozenset(
    {RULES_METHOD, LLM_METHOD, MANUAL_METHOD}
)
"""The only ``classification_method`` values that reach the wire.

Imported from :mod:`creek.classify.constants` rather than retyped: that
module exists because a rename in one writer that a reader misses is the
failure it is there to prevent.
"""

BLANK_ENTRY_REF_REASON: Final[str] = "entry_ref must not be blank"
"""Malformed-call refusal. Nothing about it is derived from vault content."""

ENTRY_REF_NOT_FOUND_REASON: Final[str] = "entry_ref not found"
"""Verbatim ``creek.reflect``'s spelling, so the surface keeps one vocabulary."""


_Record = tuple["Fragment", dict[str, object]]
"""One resolved fragment and its raw frontmatter, as the walk yields them."""


def _read_entry(
    vault_path: Path,
    entry_ref: str,
) -> tuple[PrivacyTier, _Record | None]:
    """Return the gate tier and the record of *entry_ref*, from ONE walk.

    The caller holds the content-mutation lock, so this is one consistent
    snapshot of the fragment: the tier the gate compares and the
    classification the answer reports cannot come from two different states
    of the file (#2068).

    One **exhaustive** pass — no ``break``, no early return. The list
    comprehension runs the whole walk before anything is decided, so the cost
    of a call does not depend on where in the corpus the fragment sits, or on
    whether it is there at all.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        entry_ref: The fragment id the caller addressed.

    Returns:
        ``(tier, record)``. ``tier`` is the most sensitive CURRENT persisted
        tier across every file carrying *entry_ref* — read by
        :func:`~creek.classify.privacy_filter.fragment_tier`, so a missing
        key ranks ``INTIMATE`` (#1033) — or ``INTIMATE`` when none does: with
        no evidence about what an id holds, the safe assumption is the worst
        one. ``record`` is the first match in vault walk order as
        ``(fragment, raw)``, or ``None``. A vault holding two files with the
        same id gates on the stricter of them and answers from the first;
        walk order is the same order every other consumer of the shared
        loader sees.
    """
    matches = [
        (fragment, raw)
        for _path, fragment, _body, raw in iter_vault_fragments(
            vault_path / _FRAGMENTS_SUBDIR,
        )
        if fragment.id == entry_ref
    ]
    tier = max_source_tier(starmap(fragment_tier, matches))
    return tier, (matches[0] if matches else None)


def _classification_method(raw: dict[str, object]) -> str:
    """Return the clamped provenance stamp carried by *raw*.

    Args:
        raw: The fragment file's raw frontmatter, before model defaults.

    Returns:
        One of ``rules``/``llm``/``manual`` when the stamp is present and is
        one of those exact strings, else :data:`NO_METHOD`. Absent,
        unrecognised and non-string values all collapse to the sentinel: the
        frontmatter is arbitrary user-controlled bytes and this is the clamp
        that keeps them off the wire, not merely a validation nicety.
    """
    value = raw.get(CLASSIFICATION_METHOD_KEY)
    if isinstance(value, str) and value in _PUBLISHED_METHODS:
        return value
    return NO_METHOD


def _payload(
    *,
    entry_ref: str,
    ceiling: TierCeiling,
    fragment: Fragment,
    raw: dict[str, object],
    tier: PrivacyTier,
) -> dict[str, Any]:
    """Build the eight-key success response.

    Every published value goes through :func:`str` rather than ``.value``, and
    that includes ``privacy_tier``. :class:`~creek.models.Fragment` sets
    ``use_enum_values=True``, so a field read back off a validated model is
    already a plain ``str`` and ``.value`` raises ``AttributeError`` there —
    while a field that came from the model *default* (pydantic does not
    validate defaults) is still an enum member, which an f-string would render
    as ``Frequency.UNCLASSIFIED``. The tier is the same case one step removed:
    :func:`creek.classify.privacy_filter.max_source_tier` is *typed*
    ``PrivacyTier`` and returns the enum member on its fail-closed
    ``default=INTIMATE`` path, but on the resolved path it hands back
    ``fragment.privacy_tier`` — a plain ``str`` for exactly the same reason.
    All three are ``StrEnum``, so ``str`` is the one spelling correct for both
    halves of all three.

    Args:
        entry_ref: The caller's own input, echoed.
        ceiling: The caller's own declared ceiling, echoed — never a derived
            tier.
        fragment: The resolved fragment.
        raw: That fragment's raw frontmatter, for the provenance clamp.
        tier: The tier the ceiling gate already ran against, read from the
            same locked snapshot as *fragment* and *raw* (#2068), so the
            reported ``privacy_tier`` is by construction the one that was
            gated on and belongs to the same state of the file as the
            classification beside it.

    Returns:
        The eight published keys, no more and no fewer. Every value is a
        non-null string.
    """
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "entry_ref": entry_ref,
        "frequency": str(fragment.frequency.primary),
        "phase": str(fragment.wavelength.phase),
        "privacy_tier": str(tier),
        "classification_method": _classification_method(raw),
    }


def _answer(
    vault_path: Path,
    entry_ref: str,
    ceiling: TierCeiling,
) -> dict[str, Any]:
    """Gate *entry_ref* and answer it, from one snapshot; call under the lock.

    Must run while the vault content-mutation lock is held: the gate, the
    not-found refusal and the payload all read the one snapshot
    :func:`_read_entry` takes, and the lock is what keeps a concurrent
    ``creek classify`` from rewriting the fragment while that snapshot is
    being taken (#2068).

    Args:
        vault_path: Vault root.
        entry_ref: The fragment id the caller addressed.
        ceiling: The caller's declared ceiling.

    Returns:
        The success payload, the generic above-ceiling refusal, or the
        not-found refusal — the last reachable only at a ceiling that admits
        ``INTIMATE``, because an unresolvable id reduces to it.
    """
    tier, record = _read_entry(vault_path, entry_ref)
    if (
        refusal := refuse_above_ceiling(
            tool=TOOL_NAME,
            content_tier=tier,
            ceiling=ceiling,
        )
    ) is not None:
        return refusal
    if record is None:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            reason=ENTRY_REF_NOT_FOUND_REASON,
        )
    fragment, raw = record
    return _payload(
        entry_ref=entry_ref,
        ceiling=ceiling,
        fragment=fragment,
        raw=raw,
        tier=tier,
    )


def entry_classification_tool(
    *,
    vault_path: Path,
    entry_ref: str,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Return one fragment's persisted classification, refusing above ceiling.

    The ordering is :mod:`creek_mcp.read_gate`'s, and each step is
    load-bearing:

    1. refuse a malformed call **before any vault read and without an audit
       append**, mirroring ``journal``'s ``_validated_entry_tier`` and
       ``save``/``ingest``: there is no meaningful ceiling-versus-content event
       to record yet;
    2. audit-log the attempt unconditionally and *above* the gate, recording
       the declared ceiling and consumer but never the outcome, so the trail
       cannot answer "did consumer X read fragment F?" in either direction. The
       args carry ``has_entry_ref`` rather than the id itself, and that is a
       rule with a mechanism: ``summarise_args`` passes any string of at most
       64 characters through **verbatim**, and a ``frag-`` id is about 17, so
       naming it would write every probed target into
       ``00-Creek-Meta/audit/mcp.jsonl``;
    3. under the vault content-mutation lock, take **one** exhaustive
       snapshot of the fragment — its current persisted tier and its record
       together (#2068) — refusing
       :data:`~creek.vault.mutations.CONTENT_MUTATION_BUSY_REASON` if the
       lock cannot be acquired within its bound;
    4. still inside that hold, run the ceiling gate on the snapshot's tier,
       and **only then** answer from the same snapshot.

    Args:
        vault_path: Root of the Obsidian vault.
        entry_ref: The fragment id to report on.
        privacy_tier_ceiling: The caller's declared ceiling, compared against
            the fragment's current persisted tier.
        consumer: Identifier recorded in the MCP audit log. A parameter of this
            function only — it is never on the wire; ``build_server``'s closure
            supplies it, as it does for every other tool.

    Returns:
        ``status="ok"`` with the eight published keys when the fragment is
        within the ceiling, else the canonical four-key refusal carrying one of
        :data:`BLANK_ENTRY_REF_REASON`, :data:`ENTRY_REF_NOT_FOUND_REASON`,
        :data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON` or
        :data:`~creek.vault.mutations.CONTENT_MUTATION_BUSY_REASON` (another
        writer held the content lock past its bound; retry). No other
        reason, and no extra key: anything further would be derived from
        content the caller is not admitted to.
    """
    if not entry_ref.strip():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=BLANK_ENTRY_REF_REASON,
        )
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"has_entry_ref": True},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        affected_fragment_ids=[],
    )
    with ExitStack() as hold:
        # The catch covers ACQUISITION only: nothing below raises
        # VaultLockTimeoutError, and a busy answer must never stand in for a
        # snapshot that was taken.
        try:
            hold.enter_context(vault_lock(content_mutation_lock_path(vault_path)))
        except VaultLockTimeoutError:
            return refusal_response(
                tool=TOOL_NAME,
                ceiling=privacy_tier_ceiling,
                reason=CONTENT_MUTATION_BUSY_REASON,
            )
        return _answer(vault_path, entry_ref, privacy_tier_ceiling)
