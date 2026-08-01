"""Pure policy functions for the privacy-tier pass (issue #876).

:class:`~creek.classify.privacy.PrivacyClassifier` shipped fully
implemented but with **zero production callers**, so every fragment
stayed ``privacy_tier: unclassified`` forever and Intimate-never-cloud
routing (#666 / ADR-0003) had nothing to act on. This module is the
shared, side-effect-free policy layer that both callers — the
``creek classify`` engine and the ``creek process`` pipeline — use to
close that hole:

* :func:`needs_tier` — does this raw frontmatter still owe us a tier?
* :func:`escalate` — merge two candidate tiers, never lowering.
* :func:`apply_tier` — stamp a tier, honouring a manual override.
* :func:`reassess` — re-run the check after classification hardened the
  fragment's voice signals; escalate-only. Both entry points call it,
  each as its last mutation before the write (#974).

Two rules hold across every function here:

**Never auto-downgrade.** Lowering a tier is the only direction that
leaks content, so both the ``--force`` merge and the post-classification
reassess take the *more* restrictive of the two candidates. An operator
who wants a tier relaxed edits the frontmatter by hand.

**Never yield ``unclassified``.**
:meth:`~creek.classify.privacy.PrivacyClassifier.classify_tier` always
returns one of ``open`` / ``personal`` / ``intimate`` (its final fallback
is ``personal``), and :func:`escalate` ranks ``unclassified`` *below*
``open`` so any real candidate supersedes it. Together those two
properties are what let the engine promise it never persists
``privacy_tier: unclassified``.

No vault, no I/O, no LLM: everything here is a pure function of the
fragment, its body, and the frontmatter already on disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from creek.classify.privacy_filter import tier_of
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from creek.classify.privacy import PrivacyClassifier
    from creek.models import Fragment


PRIVACY_TIER_KEY: Final[str] = "privacy_tier"
"""Frontmatter key carrying a fragment's :class:`~creek.models.PrivacyTier`."""


_ESCALATION_RANK: Final[dict[PrivacyTier, int]] = {
    PrivacyTier.UNCLASSIFIED: -1,
    PrivacyTier.OPEN: 0,
    PrivacyTier.PERSONAL: 1,
    PrivacyTier.INTIMATE: 2,
}
"""Restrictiveness ordering for :func:`escalate` — higher wins a merge.

``UNCLASSIFIED`` sits *below* ``OPEN`` here because it is the absence of
a decision, not a decision: merging it against any real tier must yield
the real one, or the engine would persist ``unclassified`` and re-open
the fail-open hole this module exists to close.

Deliberately distinct from
:data:`creek.classify.privacy_filter._TIER_RANK`, which ranks
``UNCLASSIFIED`` **at** ``PERSONAL`` because it answers a different
question: "how cautiously must a *reader* treat content nobody has
vouched for?" (cautiously — like personal) versus this table's "which of
two candidate labels is the stronger claim?" (any label beats none).
"""


def needs_tier(raw: dict[str, object]) -> bool:
    """Return whether *raw* frontmatter still owes the fragment a tier.

    Two on-disk shapes mean "never been through a privacy pass": the
    ``privacy_tier`` key is absent (the legacy / hand-written fragment),
    or it is present but set to ``unclassified`` (the pipeline default
    that :class:`~creek.models.Fragment` stamps on every fresh ingest).
    Any other value is a deliberate decision the pass must not overwrite
    without ``--force``.

    Args:
        raw: The fragment's frontmatter dict, exactly as read from disk.

    Returns:
        ``True`` when a tier still needs assigning.
    """
    if PRIVACY_TIER_KEY not in raw:
        return True
    return raw[PRIVACY_TIER_KEY] == PrivacyTier.UNCLASSIFIED.value


def escalate(current: PrivacyTier, candidate: PrivacyTier) -> PrivacyTier:
    """Return the more restrictive of *current* and *candidate*.

    The merge is symmetric in its arguments by construction (each rank
    maps to exactly one tier, so equal ranks mean equal tiers). That
    matters: an asymmetric merge would let the *caller's* argument order
    decide whether an intimate fragment gets lowered — precisely the
    failure mode this function exists to make impossible.

    Args:
        current: The tier already recorded on the fragment.
        candidate: The tier the heuristic just derived.

    Returns:
        Whichever tier ranks higher on ``open < personal < intimate``.
    """
    if _ESCALATION_RANK[candidate] > _ESCALATION_RANK[current]:
        return candidate
    return current


def apply_tier(
    fragment: Fragment,
    body: str,
    *,
    raw: dict[str, object],
    force: bool,
    classifier: PrivacyClassifier,
) -> Fragment:
    """Stamp a privacy tier on *fragment*, honouring a manual override.

    Three cases:

    1. The frontmatter owes a tier (:func:`needs_tier`) — assign the
       heuristic's verdict outright.
    2. A deliberate tier is on disk and *force* is ``False`` — return
       *fragment* untouched. The operator's call outranks the heuristic.
    3. A deliberate tier is on disk and *force* is ``True`` — merge
       escalate-only, so ``--force`` can raise a too-light tier but can
       never auto-lower an ``intimate`` one.

    The result is never ``unclassified`` (see the module docstring).

    Args:
        fragment: The fragment to tier. Never mutated.
        body: The fragment's markdown body — the classifier scans it for
            recovery keywords, which are a body-only signal that the
            :class:`~creek.models.Fragment` model does not carry.
        raw: The fragment's frontmatter as read from disk, used to tell
            "no decision yet" from "a deliberate decision".
        force: The ``--force`` flag, i.e. "re-derive tiers that are
            already set".
        classifier: The shared :class:`PrivacyClassifier`.

    Returns:
        The fragment carrying a concrete tier — the same object when the
        pass declines to touch it.
    """
    assigning = needs_tier(raw)
    if not (assigning or force):
        return fragment
    candidate = classifier.classify_tier(fragment, content=body)
    tier = candidate if assigning else escalate(tier_of(fragment), candidate)
    return classifier.enforce_tier(fragment, tier)


def reassess(
    fragment: Fragment,
    body: str,
    *,
    classifier: PrivacyClassifier,
) -> Fragment:
    """Re-derive the tier after classification and keep the stricter one.

    The pre-pass runs *before* the classifier so the per-tier router sees
    a real tier, which means it can only read the axes already on the
    fragment. When classification then hardens the signal — a
    self-authored fragment coming back ``confessional`` + ``conviction``,
    which is
    :meth:`~creek.classify.privacy.PrivacyClassifier.classify_tier`'s
    third INTIMATE trigger — this second look promotes the tier before
    the write, so the harder signal is not thrown away.

    Escalate-only: a lighter post-classification verdict can never demote
    the tier the pre-pass chose.

    Args:
        fragment: The classified fragment. Never mutated.
        body: The fragment's markdown body.
        classifier: The shared :class:`PrivacyClassifier`.

    Returns:
        The fragment at the merged tier — the same object when nothing
        escalated.
    """
    current = tier_of(fragment)
    merged = escalate(current, classifier.classify_tier(fragment, content=body))
    if merged is current:
        return fragment
    return classifier.enforce_tier(fragment, merged)
