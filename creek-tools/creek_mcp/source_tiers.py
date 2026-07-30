"""Shared, fail-closed source-tier survey for the MCP tools (#958).

This is the **one place** an MCP tool reads a fragment's classified
privacy tier in order to route an LLM call, so two tools can never
disagree about the same file. ``creek.compile`` (#848/#928) and
``creek.draft`` (#958) both name a set of source fragments, both put
those fragments' ids and titles into a prompt, and both must therefore
answer "how sensitive is this call?" the same way. A divergence would
mean one tool egressing a fragment the other keeps local — which is not
a cosmetic inconsistency but a leak.

The walk deliberately goes through
:func:`creek.vault.reader.iter_vault_fragments` — the same loader
``creek.compile.engine._load_fragments_for_compile`` uses — so the set of
files this survey inspects and the set the compile engine would actually
roll up are identical *by construction*. A bespoke ``frontmatter.load``
scan would diverge: :func:`creek.vault.reader.try_load_fragment` rejects
files whose ``type`` is not ``fragment`` and files that fail
:class:`~creek.models.Fragment` schema validation, both of which a raw
scan happily reads. That divergence would create a class of file one side
sees and the other does not — precisely the bug class the compile gate
exists to prevent. A file the shared loader skips (unreadable,
non-fragment, schema-invalid) is invisible to every caller here, and so
fails closed to whatever the caller does with an unresolved id.

Nothing in this module decides *admission*, and nothing here refuses: it
only reports tiers and reduces them. Refusing on them, and choosing what
an *empty* id list means, stay with the callers
(:mod:`creek_mcp.tier_ceiling`, :mod:`creek_mcp.tools.compile`,
:mod:`creek_mcp.tools.draft`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.models import PrivacyTier
from creek.vault.reader import iter_vault_fragments
from creek_mcp.tier_ceiling import tier_sensitivity

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from creek.models import Fragment


def fragment_tier(fragment: Fragment, raw: dict[str, object]) -> PrivacyTier:
    """Return *fragment*'s tier, failing closed when the key is absent.

    :class:`~creek.models.Fragment` defaults a *missing* ``privacy_tier``
    to ``unclassified``, which ranks alongside ``open`` and so would be
    admitted at every ceiling. Reading the tier off the model alone
    would therefore fail **open** on exactly the file whose tier nobody
    can vouch for — a hand-edited or legacy fragment. The raw
    frontmatter is consulted because it is the only place the two cases
    are still distinguishable once the model has applied its default.

    This mirrors :func:`creek_mcp.tools.reflect._fragment_tier` (#847)
    and :func:`creek.classify.privacy_filter.tier_of`, both of which
    treat an absent tier as ``intimate``. Every MCP tool must agree with
    them: two MCP tools that disagree about the same file is precisely the
    divergence this module's shared-loader design exists to prevent.

    A fragment carrying an *explicit* ``privacy_tier: unclassified`` —
    what every pipeline-written, not-yet-classified fragment has — is
    untouched here and stays admitted at any ceiling. That ranking is
    deliberate policy owned by ``creek_mcp.tier_ceiling._TIER_RANK``
    (#923), not by this fail-closed path.

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


def source_tiers(vault_path: Path, fragment_ids: Iterable[str]) -> list[PrivacyTier]:
    """Return the tiers of the *fragment_ids* that resolve, in one vault walk.

    Exactly one pass of :func:`creek.vault.reader.iter_vault_fragments`
    over ``<vault>/01-Fragments``, filtered by an id **set** so a caller
    naming a thousand ids still pays one walk of the corpus rather than a
    thousand.

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
    must be re-analysed here before it is done.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        fragment_ids: The ids whose tiers the caller needs. Duplicates
            collapse, and an id that does not resolve is simply absent
            from the result rather than an error — what "missing" means is
            the caller's policy, not this module's.

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


def max_source_tier(tiers: Iterable[PrivacyTier]) -> PrivacyTier:
    """Return the most sensitive tier in *tiers*, failing closed when empty.

    The fail-closed reduction over a :func:`source_tiers` result, kept
    here rather than repeated at each call site. Both callers were
    spelling out the same ``max(..., key=tier_sensitivity,
    default=INTIMATE)`` — and a reduction that two tools write
    separately is a reduction two tools can come to disagree about,
    which is the whole failure mode this module exists to prevent.

    Sensitivity is ranked by :func:`creek_mcp.tier_ceiling.tier_sensitivity`,
    the single ranking the MCP surface uses for both admission and
    routing, so "most sensitive" cannot mean one thing here and another
    at the gate.

    Args:
        tiers: The resolved tiers, typically straight from
            :func:`source_tiers`.

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
