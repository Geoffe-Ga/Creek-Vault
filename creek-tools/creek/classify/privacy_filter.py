"""Single source of truth for tier filtering in generation flows.

Section 13.2 of the Creek Ontology promises that intimate fragments are
excluded from generation prompts by default and that personal fragments
contribute summaries (not full bodies). This module owns the *one*
implementation of that promise so ``mine``, ``draft``, ``report``, and
``skills`` cannot drift out of agreement with each other.

An explicit ``unclassified`` tier is treated as ``PERSONAL`` throughout
(#876): both the summarising filter and the hard rank cutoff in
:func:`tier_within_override` gate it like personal content. Untiered
content is content nobody has vouched for, and it used to rank alongside
``open`` — which, while
:class:`~creek.classify.privacy.PrivacyClassifier` had no production
caller and therefore *every* fragment was untiered, meant whole private
corpora were mineable, draftable and voice-proxy eligible at the open
tier. Run ``creek classify`` so each fragment carries a deliberate tier.

:func:`tier_of` is the shared, fail-closed tier-extraction primitive (it maps an
unrecognised ``privacy_tier`` to ``INTIMATE``). It reports the tier that is
genuinely on the fragment — the ``unclassified`` → ``personal`` normalisation
above lives in :func:`_effective_tier`, not here. It is also used outside
generation — per-tier classification routing (#666) calls it to decide whether a
fragment must be classified locally — so keep it public and behaviour-stable.

Four more fail-closed primitives sit alongside it, and together they answer
"how sensitive is the call I am about to make?": :func:`tier_sensitivity`
(the reader-caution rank, read off the one :data:`_TIER_RANK` table),
:func:`fragment_tier` (a *missing* ``privacy_tier`` key — distinguishable from
an explicit ``unclassified`` only in the raw frontmatter — is ``INTIMATE``),
:func:`source_tiers` (one vault walk surveying the fragments a call names) and
:func:`max_source_tier` (the reduction over the tiers a call would carry,
``INTIMATE`` when empty).

:func:`raw_privacy_tier` and :func:`within_ceiling` (#968) are the
raw-frontmatter siblings of that pair, for generation flows that never build a
:class:`~creek.models.Fragment` at all. They live here, and not in a new
module, for the same reason everything above does: a tier reader somewhere
else is a tier opinion the others can drift from.

The last three moved here from ``creek_mcp.source_tiers`` in #962, which they
emptied, so that module is gone and this is now the single home for the
survey. They had to move because :mod:`creek.compile.engine` derives its own
LLM routing tier — the ``creek compile`` CLI has no MCP wrapper to derive one
for it, which is exactly how the ``Intimate``-never-cloud invariant (#647)
came to be unenforced there — and ``creek`` may never import ``creek_mcp``.
That layering rule is the one pinned at ``creek/ingest/journal_staging.py:10``
and ``creek/care/__init__.py:5``: ``creek_mcp`` imports ``creek``, never the
reverse, so anything both layers need lives on the ``creek`` side. Keeping the
survey whole rather than splitting it across the boundary is the point — it
was extracted in #958 precisely so ``creek.compile`` and ``creek.draft`` could
not come to disagree about the same file, and a split would have re-opened
that seam from the other direction.

Nothing here decides *admission* and nothing here refuses: these four only
report tiers and reduce them. Refusing on them, and choosing what an *empty*
id list means, stay with the callers (``creek.compile.engine``,
``creek_mcp.tier_ceiling``, ``creek_mcp.tools.compile``,
``creek_mcp.tools.draft``).

The filter accepts an optional :class:`PrivacyTierOverride` representing
the operator-supplied ``--include-tier`` flag. When the override raises
the included tier above the default, the caller is responsible for
writing an entry to the privacy audit log via
:func:`record_privacy_override`; the helper exposes
:func:`override_elevates` so callers can detect when an audit is owed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from creek.audit import AuditLog
from creek.models import PrivacyTier
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from creek.models import Fragment

logger = logging.getLogger(__name__)


PRIVACY_AUDIT_RELPATH = Path("00-Creek-Meta/audit/privacy.jsonl")
"""Canonical privacy-override audit log location under the vault root."""


class PrivacyTierOverride(StrEnum):
    """``--include-tier`` flag values.

    The values are ordered so that ``OPEN`` is the most restrictive
    (default) and ``ALL`` is the broadest override; the comparison is
    done by name, not lexically.
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    ALL = "all"


def override_elevates(
    override: PrivacyTierOverride | None,
) -> TypeGuard[PrivacyTierOverride]:
    """Return whether *override* expands access beyond the default.

    The default policy is "include open + personal-as-summary, exclude
    intimate". Anything other than ``None`` or ``OPEN`` raises the bar
    and therefore obliges the caller to write an audit entry.

    Returns a :class:`~typing.TypeGuard` so callers that branch on this
    predicate get the narrowed non-``None`` type for free — encoding the
    fact that ``None`` and ``OPEN`` are operationally equivalent at the
    type level prevents the redundant ``override is None`` guard the
    PR #193 review flagged from ever reappearing.
    """
    if override is None:
        return False
    return override is not PrivacyTierOverride.OPEN


def _summarize_personal(fragment: Fragment) -> str:
    """Replace a personal fragment's body with a title-only summary.

    Title-only is the v1 contract documented in SEC-006; richer summaries
    can land later without changing the call sites.
    """
    title = fragment.title.strip() or fragment.id
    return f"[Personal-tier summary: {title}]"


def _allows_intimate(override: PrivacyTierOverride | None) -> bool:
    """Return ``True`` when intimate fragments may pass through."""
    return override in (PrivacyTierOverride.INTIMATE, PrivacyTierOverride.ALL)


def _allows_full_personal_body(override: PrivacyTierOverride | None) -> bool:
    """Return ``True`` when personal fragments contribute their full body."""
    return override in (
        PrivacyTierOverride.PERSONAL,
        PrivacyTierOverride.INTIMATE,
        PrivacyTierOverride.ALL,
    )


_TIER_RANK: dict[PrivacyTier, int] = {
    PrivacyTier.OPEN: 0,
    # #876: an untiered fragment ranks with PERSONAL, not OPEN. It is
    # content nobody has vouched for, and before ``creek classify`` grew a
    # privacy caller *every* fragment in a vault was untiered — so ranking
    # it alongside ``open`` exposed the whole private corpus at the default
    # ceiling. Note this is the *reader's* caution ordering; the
    # escalate-only merge in :mod:`creek.classify.privacy_pass` ranks
    # ``UNCLASSIFIED`` lowest instead, because there it means "no claim
    # made" rather than "handle carefully".
    PrivacyTier.UNCLASSIFIED: 1,
    PrivacyTier.PERSONAL: 1,
    PrivacyTier.INTIMATE: 2,
}

_OVERRIDE_RANK: dict[PrivacyTierOverride, int] = {
    PrivacyTierOverride.OPEN: 0,
    PrivacyTierOverride.PERSONAL: 1,
    PrivacyTierOverride.INTIMATE: 2,
    PrivacyTierOverride.ALL: 3,
}


def tier_sensitivity(tier: PrivacyTier) -> int:
    """Return the reader-caution rank of *tier*, failing closed.

    Reads :data:`_TIER_RANK` — the ordering this module already applies in
    :func:`tier_within_override` — rather than introducing a second table,
    so "how sensitive is this?" cannot come to mean one thing for the hard
    cutoff and another for LLM routing. :func:`max_source_tier` reduces
    with it, which is how :mod:`creek.compile.engine` derives a routing
    tier without importing the MCP layer.

    A tier the table has never heard of is a tier nobody can vouch for, so
    it ranks *with* ``intimate`` rather than defaulting to ``0`` and being
    routed to a cloud provider. That is the same fail-closed reflex
    :func:`tier_of` applies to an unrecognised ``privacy_tier`` string, and
    it is the reason this is a lookup with a default rather than a bare
    ``_TIER_RANK[tier]`` (which would raise across a caller's boundary) or
    a ``.get(tier, 0)`` (which would fail open).

    **Do not merge this with** :func:`creek_mcp.tier_ceiling.tier_sensitivity`.
    The two agree tier for tier today, and that agreement is load-bearing
    now that MCP *routing* ranks through this table while MCP *admission*
    still ranks through the MCP's — ``tests/test_mcp_tier_ceiling.py``'s
    ``test_privacy_filter_and_mcp_tier_sensitivity_agree_on_every_tier``
    asserts it rather than leaving it to coincidence. Asserting the
    agreement is exactly what lets the two stay two deliberate
    declarations. Four rank tables in this codebase answer four different
    questions with the same word ``unclassified``, and two of them rank it
    differently *on purpose* (the escalate-only merge below ``open``, the
    Writing Desk leak gate above ``intimate``); the same file's
    ``test_unclassified_ranks_differ_by_context_on_purpose``
    pins all four with the reasons attached. Collapsing them into one
    "obvious" ranking is a privacy regression wearing a cleanup's costume.

    Args:
        tier: The tier to rank.

    Returns:
        ``0`` for ``open``; ``1`` for ``personal`` and ``unclassified``
        (#876); ``2`` for ``intimate`` and for any tier not in the table.
    """
    return _TIER_RANK.get(tier, _TIER_RANK[PrivacyTier.INTIMATE])


def tier_within_override(
    tier: PrivacyTier,
    override: PrivacyTierOverride | None,
) -> bool:
    """Return whether a *tier* fragment is admitted under *override* (hard cutoff).

    Unlike :func:`filter_fragments` — which *summarises* ``PERSONAL`` bodies and
    only drops ``INTIMATE`` — this is a strict rank cutoff that **excludes**
    anything above the override entirely. The Writing Desk needs its evidence to
    omit above-ceiling fragments outright (#660), not carry summaries. ``None``
    defaults to ``OPEN`` (the most restrictive); ``ALL`` admits every tier.
    ``UNCLASSIFIED`` ranks with ``PERSONAL`` (#876), so an untiered fragment
    needs an explicit ``personal`` ceiling to be admitted.

    Args:
        tier: The fragment's privacy tier.
        override: The admission ceiling, or ``None`` for ``OPEN``.

    Returns:
        ``True`` when the fragment may enter the evidence.
    """
    effective = override or PrivacyTierOverride.OPEN
    if effective is PrivacyTierOverride.ALL:
        return True
    return _TIER_RANK[tier] <= _OVERRIDE_RANK[effective]


def _effective_tier(fragment: Fragment) -> PrivacyTier:
    """Return the tier :func:`filter_fragments_by_tier` should enforce.

    Normalises ``UNCLASSIFIED`` to ``PERSONAL`` (#876) so the body-level
    filter agrees with the rank cutoff in :func:`tier_within_override`,
    which reads the same equivalence out of :data:`_TIER_RANK`. Kept
    separate from :func:`tier_of` deliberately: ``tier_of`` is the
    router's and the fidelity ladder's input (#666) and must keep
    reporting the tier that is genuinely on the fragment.
    """
    tier = tier_of(fragment)
    if tier is PrivacyTier.UNCLASSIFIED:
        return PrivacyTier.PERSONAL
    return tier


def filter_fragments_by_tier(
    fragments: Iterable[tuple[Fragment, str]],
    *,
    override: PrivacyTierOverride | None = None,
) -> Iterator[tuple[Fragment, str]]:
    """Yield ``(fragment, body)`` pairs honouring tier policy.

    Default behaviour:

    * ``intimate`` → excluded.
    * ``personal`` → included with body replaced by a title-only summary.
    * ``open`` / ``public`` → included with full body.
    * ``unclassified`` → treated as ``personal`` (#876): still yielded,
      so the operator sees the fragment exists, but contributing a
      title-only summary rather than its raw body. Untiered content is
      content nobody has vouched for; presuming it non-sensitive handed
      every un-classified vault's private corpus to the generation flows
      at the open tier. Run ``creek classify`` to give each fragment a
      deliberate tier, or raise the ceiling explicitly.

    Override semantics:

    * ``OPEN`` (or ``None``): default behaviour.
    * ``PERSONAL``: personal bodies pass through unredacted; intimate
      remains excluded.
    * ``INTIMATE`` / ``ALL``: every tier passes through with its full
      body.

    Args:
        fragments: Iterable of ``(fragment, body)`` pairs from the
            caller's vault scan.
        override: Optional :class:`PrivacyTierOverride` from
            ``--include-tier``.
    """
    for fragment, body in fragments:
        tier = _effective_tier(fragment)
        if tier == PrivacyTier.INTIMATE and not _allows_intimate(override):
            continue
        if tier == PrivacyTier.PERSONAL and not _allows_full_personal_body(override):
            yield fragment, _summarize_personal(fragment)
            continue
        yield fragment, body


def tier_of(fragment: Fragment) -> PrivacyTier:
    """Return the fragment's privacy tier as a :class:`PrivacyTier`.

    Pydantic's :class:`~creek.models.Fragment` validator constrains
    ``privacy_tier`` to the enum values, so unrecognised strings should
    not normally reach this helper. The defensive ``except`` exists for
    fragments that bypassed Pydantic validation (e.g. legacy data hand-
    edited in the vault, or a future schema migration that adds a tier
    we don't yet know about). Failing closed to ``INTIMATE`` ensures an
    unknown classification is treated as the most-restrictive tier
    rather than silently defaulting to ``open`` and exposing the body.
    """
    try:
        return PrivacyTier(fragment.privacy_tier)
    except ValueError:
        logger.warning(
            "Fragment %s carries unrecognised privacy_tier %r; "
            "treating as INTIMATE for fail-closed filtering. "
            "Re-run `creek classify` to assign a recognised tier.",
            fragment.id,
            fragment.privacy_tier,
        )
        return PrivacyTier.INTIMATE


def fragment_tier(fragment: Fragment, raw: dict[str, object]) -> PrivacyTier:
    """Return *fragment*'s tier, failing closed when the key is absent.

    :class:`~creek.models.Fragment` defaults a *missing* ``privacy_tier``
    to ``unclassified``, which ranks with ``personal`` (#876, extended to
    the MCP ceiling by #961) and so would be admitted at ``personal`` and
    above, but refused at ``open``. Reading the tier off the model alone
    would therefore still fail open relative to the raw file at an
    ``open`` ceiling — #961 narrowed the blast radius, but did not remove
    the need to read the raw frontmatter: a *missing* key must still be
    distinguished from an *explicit* ``unclassified`` and fail all the way
    closed to INTIMATE, since a hand-edited or legacy fragment with no key
    at all carries even less assurance than a pipeline-written one that at
    least says ``unclassified`` out loud. The raw frontmatter is consulted
    because it is the only place the two cases are still distinguishable
    once the model has applied its default.

    This sits directly beneath :func:`tier_of` because the two are halves
    of one fail-closed reading: ``tier_of`` fails closed on a tier string
    it does not recognise, this one on a tier that was never written down.
    :func:`creek_mcp.tools.reflect._fragment_tier` (#847) is the third,
    and every caller of the three must agree: two tools that disagree
    about the same file is precisely the divergence the shared-loader
    design of :func:`source_tiers` below exists to prevent.

    It was written in ``creek_mcp.source_tiers`` (#958) and moved here in
    #962 — not into ``reflect``, which keeps its own copy — because
    :mod:`creek.compile.engine` needs it to derive its own routing tier
    and ``creek`` may never import ``creek_mcp``.

    A fragment carrying an *explicit* ``privacy_tier: unclassified`` —
    what every pipeline-written, not-yet-classified fragment has — is
    untouched here and now needs a ``personal`` ceiling to be admitted.
    That ranking is deliberate policy owned by :data:`_TIER_RANK` and its
    MCP counterpart ``creek_mcp.tier_ceiling._TIER_RANK`` (#961), not by
    this fail-closed path.

    Args:
        fragment: The validated fragment as loaded by the shared reader.
        raw: The file's raw frontmatter, before model defaults applied.

    Returns:
        ``PrivacyTier.INTIMATE`` when ``privacy_tier`` is absent from
        *raw*, else the fragment's own classified tier.
    """
    if "privacy_tier" not in raw:
        return PrivacyTier.INTIMATE
    return fragment.privacy_tier


def raw_privacy_tier(raw: Mapping[str, object]) -> PrivacyTier:
    """Return a vault note's tier read straight off raw frontmatter, failing closed.

    The raw-frontmatter sibling of :func:`fragment_tier`, and deliberately
    *not* a merge with it. ``fragment_tier`` needs a validated
    :class:`~creek.models.Fragment`, and the report generators #968 had to
    gate do not all have one:
    :class:`creek.generate.tags.TagGardenGenerator` builds no model at
    all — ``_extract_tags`` is a bare ``frontmatter.load`` — and it scans
    four directories (``02-Threads``, ``03-Eddies``, ``04-Praxis``,
    ``08-Decisions``) whose note types have no ``privacy_tier`` field in
    their models in the first place. Asking those files for a ``Fragment``
    would mean inventing one.

    The two readers are pinned equal for every fragment by
    ``tests/test_mcp_report_tier_ceiling.py``'s
    ``test_raw_and_model_tier_readers_agree_on_every_fragment``, walking the
    shared vault loader tier by tier. That pin is what stops this reader
    becoming the third, diverging tier opinion the module docstring above
    warns about — "two tools that disagree about the same file" is a bug
    class, not a style question, and an assertion is the only thing that
    keeps two deliberate implementations honest about agreeing.

    A *missing* key resolves to ``INTIMATE`` rather than to the model's
    ``unclassified`` default, and the distinction is the whole point. An
    explicit ``unclassified`` ranks with ``personal`` (#876/#961), so it is
    *admitted* at ``ceiling=personal``; reading a missing key through the
    model would therefore fail open relative to what the file actually says.
    A hand-edited or legacy note with no key at all carries less assurance
    than a pipeline-written one that says ``unclassified`` out loud.

    Args:
        raw: The note's raw frontmatter, before any model defaults are
            applied. ``frontmatter.Post.metadata`` satisfies this directly.

    Returns:
        ``PrivacyTier.INTIMATE`` when ``privacy_tier`` is absent, ``None``,
        empty, or a string the enum does not recognise; otherwise the
        declared tier.
    """
    value = raw.get("privacy_tier")
    if value is None or value == "":
        return PrivacyTier.INTIMATE
    try:
        return PrivacyTier(str(value))
    except ValueError:
        logger.warning(
            "Vault note carries unrecognised privacy_tier %r; "
            "treating as INTIMATE for fail-closed filtering. "
            "Re-run `creek classify` to assign a recognised tier.",
            value,
        )
        return PrivacyTier.INTIMATE


def within_ceiling(raw: Mapping[str, object], override: PrivacyTierOverride) -> bool:
    """Return whether the note described by *raw* is admitted under *override*.

    The admission gate the six ``creek report`` generators share (#968).
    It reduces to :func:`tier_within_override` — the **hard rank cutoff** —
    rather than to :func:`filter_fragments_by_tier`, and that choice is not
    incidental:

    * Three of the six generators (``tags``, ``decisions``, ``mode-profiles``)
      never read a body at all. They read frontmatter ``tags`` / ``id``, the
      ``title``, and ``wavelength.mode``. Summarising a ``PERSONAL`` *body*
      there is a literal no-op — a gesture, not a gate — while the title and
      tags it does read would sail straight through.
    * For the three that do read bodies (``voice``, ``lexicon``,
      ``rhetorical-patterns``) summarisation is worse than useless: the
      ``"[Personal-tier summary: <title>]"`` stub would be written into
      ``### Sample Passages`` as a *voice exemplar*, leaking the title and
      poisoning the voice corpus with a synthetic sentence in nobody's voice.

    A report must omit. Note this gate is additive to, not a replacement for,
    the ``allow_intimate`` consent gate in :mod:`creek.generate.voice`:
    admission is ``allow_intimate`` **and** ``within_ceiling``.

    Args:
        raw: The note's raw frontmatter, as loaded by the caller.
        override: The admission ceiling. ``PrivacyTierOverride.ALL`` admits
            everything, which is what "no ceiling declared" means for the
            library's existing callers.

    Returns:
        ``True`` when the note may contribute to the artifact being written.
    """
    return tier_within_override(raw_privacy_tier(raw), override)


def max_source_tier(tiers: Iterable[PrivacyTier]) -> PrivacyTier:
    """Return the most sensitive tier in *tiers*, failing closed when empty.

    The fail-closed reduction over the tiers of the fragments one LLM call
    would carry, kept in one place rather than repeated at each call site.
    Its callers — ``creek.compile.engine._routing_tier_for`` and
    ``creek_mcp.tools.draft._source_routing_tier`` — were otherwise each
    spelling out the same ``max(..., key=tier_sensitivity,
    default=INTIMATE)``, and a reduction that two tools write separately
    is a reduction two tools can come to disagree about, which is the
    whole failure mode the shared source-tier survey exists to prevent.

    Sensitivity is ranked by :func:`tier_sensitivity`, so this is the
    *reader's* caution ordering — the same one :func:`tier_within_override`
    cuts off on — and "most sensitive" cannot mean one thing here and
    another at an admission gate. The MCP surface admits through its own
    table, and ``tests/test_mcp_tier_ceiling.py``'s
    ``test_privacy_filter_and_mcp_tier_sensitivity_agree_on_every_tier``
    pins the two rankings equal so an MCP call cannot be admitted under
    one ordering and routed under another.

    Args:
        tiers: The resolved tiers of the fragments a call would carry,
            typically from :func:`source_tiers` or from the compile
            engine's own load.

    Returns:
        The most sensitive tier present, or
        :attr:`~creek.models.PrivacyTier.INTIMATE` when *tiers* is empty.
        Empty means no requested id resolved, and with no evidence about
        what a call would carry the safe assumption is the worst one. A
        caller for which "empty" can also mean "nothing was asked for"
        must separate that case out *before* calling — see
        :func:`creek_mcp.tools.draft._source_routing_tier`.
    """
    return max(tiers, key=tier_sensitivity, default=PrivacyTier.INTIMATE)


def source_tiers(vault_path: Path, fragment_ids: Iterable[str]) -> list[PrivacyTier]:
    """Return the tiers of the *fragment_ids* that resolve, in one vault walk.

    Exactly one pass of :func:`creek.vault.reader.iter_vault_fragments`
    over ``<vault>/01-Fragments``, filtered by an id **set** so a caller
    naming a thousand ids still pays one walk of the corpus rather than a
    thousand.

    The walk deliberately goes through that shared loader — the same one
    ``creek.compile.engine._load_fragments_for_compile`` uses — so the set
    of files this survey inspects and the set the compile engine would
    actually roll up are identical *by construction*. A bespoke
    ``frontmatter.load`` scan would diverge:
    :func:`creek.vault.reader.try_load_fragment` rejects files whose
    ``type`` is not ``fragment`` and files that fail
    :class:`~creek.models.Fragment` schema validation, both of which a raw
    scan happily reads. That divergence would create a class of file one
    side sees and the other does not — precisely the bug class the compile
    gate exists to prevent. A file the shared loader skips (unreadable,
    non-fragment, schema-invalid) is invisible to every caller here, and so
    fails closed to whatever the caller does with an unresolved id.

    **The walk never short-circuits, and that property is load-bearing.**
    ``iter_vault_fragments`` materialises the whole directory into a list
    before returning, so the rglob + parse cost is paid in full before the
    filter below runs at all; the filter then runs to completion,
    collecting every requested fragment's tier before the caller decides
    anything. The cost of a call is therefore uniform *by construction*,
    not by accident of iteration order — which is what stops
    ``creek.compile``'s deliberately content-free above-ceiling refusal
    (``creek_mcp.tools.compile._ABOVE_CEILING_REASON``) from leaking
    *where* the offending fragment sits through timing. Contrast
    ``creek_mcp.tools.reflect._resolve_entry``, which walks a raw lazy
    ``rglob`` and returns at the match: a genuine timing channel, which
    the comment there correctly records. Switching this function to a lazy
    loader would open that same channel for every caller at once, so it
    must be re-analysed here before it is done. That the analysis is about
    an MCP refusal while the function now lives in ``creek`` is not an
    argument for splitting the two apart: the property belongs to *this*
    loop, and only stays true if it is stated where the loop is.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        fragment_ids: The ids whose tiers the caller needs. Duplicates
            collapse, and an id that does not resolve is simply absent
            from the result rather than an error — what "missing" means is
            the caller's policy, not this function's.

    Returns:
        One :class:`~creek.models.PrivacyTier` per *resolved* id, in vault
        walk order. The list is shorter than *fragment_ids* whenever an id
        does not resolve, and an **empty list is ambiguous**: it means
        either "nothing was asked for" or "nothing asked for resolved".
        Those two cases must route differently, so a caller that cannot
        tell them apart from the result alone has to separate them before
        calling (``creek.draft``) or fail closed to
        :attr:`~creek.models.PrivacyTier.INTIMATE` (``creek.compile``).
    """
    requested = set(fragment_ids)
    return [
        fragment_tier(fragment, raw)
        for _path, fragment, _body, raw in iter_vault_fragments(
            vault_path / "01-Fragments",
        )
        if fragment.id in requested
    ]


def record_privacy_override(
    *,
    vault_path: Path,
    command: str,
    fragment_ids: Iterable[str],
    operator: str,
    override: PrivacyTierOverride,
) -> None:
    """Append a privacy-override audit entry to ``audit/privacy.jsonl``.

    Args:
        vault_path: Vault root under which the audit log lives.
        command: CLI subcommand name (e.g. ``"mine"``, ``"draft"``).
        fragment_ids: Fragment IDs included by the override.
        operator: Identity of the operator that issued the override.
        override: The :class:`PrivacyTierOverride` value that was
            applied.
    """
    log = AuditLog(vault_path / PRIVACY_AUDIT_RELPATH)
    payload: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "command": command,
        "operator": operator,
        "include_tier": override.value,
        "fragment_ids": list(fragment_ids),
    }
    log.append(payload)


@dataclass(frozen=True)
class PreSaveFilterResult:
    """Outcome of :func:`pre_save_filter`.

    Attributes:
        vault_body: Markdown body to write into the vault note. For
            non-open tiers this is a title-only summary.
        stub_body: Full body destined for the gitignored intimate-stub
            file, or ``None`` when the tier does not require off-vault
            stashing.
        stub_relpath: Vault-relative path under which the stub will be
            written (``10-Liminal/Compost/intimate-stubs/<slug>.md``),
            or ``None`` when no stub is needed.
    """

    vault_body: str
    stub_body: str | None
    stub_relpath: Path | None


def _title_only_summary(title: str | None) -> str:
    """Return the body that gets written when only the title is safe."""
    safe_title = (title or "").strip() or "(untitled)"
    return f"[Tier-redacted summary: {safe_title}]\n"


def _stub_relpath_for(title: str | None) -> Path:
    """Compose the gitignored stub path for an intimate body."""
    from creek.save._constants import INTIMATE_STUB_RELPATH
    from creek.save._slug import slugify_filename

    raw = (title or "intimate").strip().lower() or "intimate"
    slug = slugify_filename(raw) or "intimate"
    return INTIMATE_STUB_RELPATH / f"{slug}.md"


def pre_save_filter(
    body: str,
    *,
    tier: PrivacyTier,
    title: str | None,
    full_body: bool = False,
) -> PreSaveFilterResult:
    """Apply tier-aware redaction to a ``creek save`` body.

    The contract follows FEAT-009's "privacy enforcement" block:

    * ``open`` — full body is written into the vault.
    * ``personal`` — body is replaced with a title-only summary unless
      *full_body* is explicitly ``True``.
    * ``intimate`` — body is replaced with a title-only summary in the
      vault, and the full body is routed to the gitignored
      ``10-Liminal/Compost/intimate-stubs/`` directory.

    Args:
        body: The raw answer body the operator wants to file back.
        tier: Privacy tier inherited from provenance or supplied via
            ``--tier``.
        title: Optional title — used to compose the title-only summary
            and the stub filename.
        full_body: When ``True``, allow personal-tier bodies through
            unredacted. Ignored for ``intimate``.

    Returns:
        A :class:`PreSaveFilterResult` describing what to write where.
    """
    if tier == PrivacyTier.INTIMATE:
        return PreSaveFilterResult(
            vault_body=_title_only_summary(title),
            stub_body=body,
            stub_relpath=_stub_relpath_for(title),
        )
    if tier == PrivacyTier.PERSONAL and not full_body:
        return PreSaveFilterResult(
            vault_body=_title_only_summary(title),
            stub_body=None,
            stub_relpath=None,
        )
    return PreSaveFilterResult(
        vault_body=body,
        stub_body=None,
        stub_relpath=None,
    )


def parse_include_tier(value: str | None) -> PrivacyTierOverride | None:
    """Parse a CLI ``--include-tier`` value into the typed enum.

    Returns ``None`` for an unset flag so the call site can short-circuit
    without a noisy comparison; raises :class:`ValueError` with the
    canonical option list when the value is malformed so the CLI can
    re-raise with ``typer.Exit(2)``.
    """
    if value is None:
        return None
    try:
        return PrivacyTierOverride(value.lower())
    except ValueError as exc:
        msg = (
            f"Unknown --include-tier {value!r}. "
            f"Use one of: {', '.join(member.value for member in PrivacyTierOverride)}."
        )
        raise ValueError(msg) from exc
