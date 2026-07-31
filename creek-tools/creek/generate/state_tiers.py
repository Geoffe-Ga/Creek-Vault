"""Tier admission and self-description for the ``creek state`` artifact (#969).

``creek state`` writes a document, and until #969 that document said nothing
about how sensitive its own contents were. ``creek.state.read`` therefore had
nothing to compare a caller's ceiling against and served
``00-Creek-Meta/State/latest.md`` verbatim at every ceiling. This module holds
the two halves of the fix that are *pure* — the artifact stamp and the
reductions the per-section gates in :mod:`creek.generate.state` need — so that
module keeps its per-function complexity and per-file coverage budget while the
admission decisions stay next to the sections that make them.

**The stamp key is ``privacy_tier``, deliberately.** A bespoke key would have
forced a bespoke reader, and a second tier reader is exactly the divergence
:mod:`creek.classify.privacy_filter`'s module docstring warns about ("two tools
that disagree about the same file"). Writing the tier under the name every
other vault note uses means :func:`stamped_content_tier` can delegate to
:func:`creek.classify.privacy_filter.raw_privacy_tier` verbatim, inheriting its
fail-closed handling of a missing, empty, non-string or unrecognised value.
#969 adds no tier reader of its own.

**The stamp is three scalars and nothing else.** CrawDad keeps a report's
``raw_markdown`` *including* its frontmatter and feeds it into prompts
(``crawdad/crawdad/state.py``), and its bullet regex is ``^\\s*-\\s+``; a
block-style YAML list in the stamp would be misread as a report bullet. So the
dump is ``yaml.safe_dump(..., sort_keys=False)`` over scalar values only, which
produces three inert ``key: value`` lines. CrawDad's ``_split_sections``
discards everything before the first ``## ``, so the stamp is invisible to its
section parsing as well.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.classify.privacy_filter import raw_privacy_tier, tier_sensitivity
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Iterable

    from creek.classify.privacy_filter import PrivacyTierOverride

logger = logging.getLogger(__name__)


STATE_REPORT_TYPE: str = "state-report"
"""Value written under the stamp's ``type`` key.

Chosen so a stamped report cannot be mistaken for vault content by the very
generator that wrote it: ``creek.generate.state._load_typed_models`` admits
only ``fragment`` / ``thread`` / ``eddy`` / ``praxis``, and
``creek.generate.tags.TagGardenGenerator`` does not scan ``00-Creek-Meta``.
"""

TYPE_STAMP_KEY: str = "type"
"""Frontmatter key carrying :data:`STATE_REPORT_TYPE`."""

TIER_STAMP_KEY: str = "privacy_tier"
"""Frontmatter key carrying the highest tier the render actually admitted.

This is what ``creek.state.read`` compares the caller's ceiling against — not
the ceiling the render ran under. Comparing the render ceiling would refuse a
broad render over a narrow corpus for no reason: a report produced at
``--include-tier all`` over a vault holding nothing above ``open`` contains
nothing above ``open`` and must stay readable at ``ceiling=open``.
"""

CEILING_STAMP_KEY: str = "tier_ceiling"
"""Frontmatter key recording the override the render ran under.

Kept for the audit trail and deliberately *not* consulted by the read gate;
see :data:`TIER_STAMP_KEY` for why.
"""


def derived_link_tiers(
    entries: Iterable[tuple[PrivacyTier, Iterable[str]]],
) -> dict[str, list[PrivacyTier]]:
    """Group source tiers by the wikilink target each source names.

    :class:`~creek.models.Eddy` and :class:`~creek.models.Thread` carry no
    ``privacy_tier`` field of their own, so the only evidence about how
    sensitive an eddy title is comes from the fragments that name it. This
    builds that evidence, and it must be built over the **unfiltered** fragment
    corpus: an eddy with one ``open`` and one ``intimate`` member is
    ``intimate``, and reducing over the *already-admitted* fragments alone
    would resolve it to ``open`` and leak the title at ``ceiling=open``.

    The reduction itself is left to
    :func:`creek.classify.privacy_filter.max_source_tier`, whose empty case
    (``INTIMATE``) is the right answer for a target no fragment names: nobody
    has vouched for it.

    Args:
        entries: ``(source tier, wikilink targets)`` pairs, one per fragment.

    Returns:
        ``{target: [tier, ...]}``. A target absent from the mapping had no
        constituents at all, which the caller must read as "no evidence" rather
        than as "no sensitivity".
    """
    grouped: dict[str, list[PrivacyTier]] = {}
    for tier, targets in entries:
        for target in targets:
            grouped.setdefault(target, []).append(tier)
    return grouped


def max_admitted_tier(tiers: Iterable[PrivacyTier]) -> PrivacyTier:
    """Return the most sensitive tier the render admitted, ``OPEN`` when none.

    Ranked by :func:`creek.classify.privacy_filter.tier_sensitivity` — the same
    reader's-caution ordering the admission cutoff uses — so "most sensitive"
    cannot mean one thing at the gate and another on the stamp.

    **The empty default is ``OPEN``, and that is the one place this reduction
    deliberately diverges from
    :func:`creek.classify.privacy_filter.max_source_tier`.** The two empties
    describe different situations. ``max_source_tier``'s empty means "ids were
    named and none of them resolved" — an absence of *knowledge* about what a
    call would carry, so the safe assumption is the worst one. Here empty means
    "the render admitted nothing and wrote no content-derived bytes" — which is
    knowledge, and it is negative. Stamping such a report ``intimate`` would
    make a freshly-initialised vault's first report unreadable at every ceiling
    below ``intimate``: an outage on first run, and precisely what #969's
    recoverability criterion forbids.

    Args:
        tiers: Tiers of every item the render actually put in the document.

    Returns:
        The most sensitive tier present, or
        :attr:`~creek.models.PrivacyTier.OPEN` when *tiers* is empty.
    """
    return max(tiers, key=tier_sensitivity, default=PrivacyTier.OPEN)


def stamp_report(
    body: str,
    *,
    content_tier: PrivacyTier,
    override: PrivacyTierOverride,
) -> str:
    """Prefix a rendered report with its self-describing YAML stamp.

    The body is passed through byte for byte; only a frontmatter block is
    prepended. Nothing in the report is re-encoded, so em-dashes, backticks and
    ``##`` headings survive the round trip that :func:`stamped_content_tier`
    later performs.

    Args:
        body: The rendered markdown document, unstamped.
        content_tier: The highest tier the render admitted — see
            :data:`TIER_STAMP_KEY`.
        override: The ceiling the render ran under, recorded for the audit
            trail under :data:`CEILING_STAMP_KEY`.

    Returns:
        ``---\\n<three scalar lines>\\n---\\n\\n<body>``.
    """
    stamp = {
        TYPE_STAMP_KEY: STATE_REPORT_TYPE,
        TIER_STAMP_KEY: content_tier.value,
        CEILING_STAMP_KEY: override.value,
    }
    # ``sort_keys=False`` keeps type/tier/ceiling in the order a human reads
    # them; ``default_flow_style=False`` keeps every value on its own line.
    front = yaml.safe_dump(stamp, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n\n{body}"


def stamped_content_tier(text: str) -> PrivacyTier:
    """Read a state artifact's stamped tier, failing closed to ``INTIMATE``.

    Three separate failures land on the same answer, and that identity is
    load-bearing rather than lazy: a report with unparsable frontmatter, a
    report with no stamp at all, and a report stamped with a value the enum
    does not recognise are all reports nobody has vouched for. Distinguishing
    them in the *result* would be harmless, but distinguishing them in the
    caller's refusal would turn the gate into an oracle for whether the vault
    holds above-ceiling content, so they are collapsed here where the reasoning
    belongs.

    The unstamped case is the pre-#969 ``latest.md``, and ``INTIMATE`` is an
    accurate statement about it rather than a cautious one: every report written
    before this change was rendered completely unfiltered — the equivalent of
    ``--include-tier all``. Recovery is one command
    (``creek.state.render``/``creek state --include-tier <t>``), and no ceiling
    of ``all`` ever refuses it, so nothing is permanently unreachable.

    :class:`ValueError` is caught alongside :class:`yaml.YAMLError`, and it is
    not defensive padding: ``frontmatter.loads`` dispatches on *handler*, and
    the library registers a ``JSONHandler`` whose boundary pattern is
    ``^(?:{|})$``. An artifact whose first line is exactly ``{`` is therefore
    routed to :func:`json.loads`, which raises :class:`json.JSONDecodeError` —
    a ``ValueError``, not a ``yaml.YAMLError``. Catching only the latter let
    that escape uncaught and crash ``creek_mcp.tools.state_read`` instead of
    failing closed, which is the single outcome this function exists to
    prevent. The two exception types are unrelated in the hierarchy, so both
    must be named.

    ``frontmatter.loads`` does already handle the other two ways a document
    can disappoint us without raising: an artifact with no frontmatter
    delimiters at all yields ``{}`` (which :func:`raw_privacy_tier` reads as
    ``INTIMATE``), and a frontmatter block that parses to a list or a scalar is
    discarded by its own ``isinstance(fm_data, dict)`` guard, yielding ``{}``
    likewise. Neither needs an ``except``.

    Args:
        text: The artifact's full bytes, stamp included.

    Returns:
        The stamped :class:`~creek.models.PrivacyTier`, or
        :attr:`~creek.models.PrivacyTier.INTIMATE` when the stamp is missing or
        unreadable.
    """
    try:
        metadata = frontmatter.loads(text).metadata
    except (yaml.YAMLError, ValueError):
        logger.debug("State artifact frontmatter is unparsable; failing closed")
        return PrivacyTier.INTIMATE
    return raw_privacy_tier(metadata)
