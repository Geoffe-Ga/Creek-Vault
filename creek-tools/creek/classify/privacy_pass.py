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
* :func:`needs_retier` — is a tier already on record *less* restrictive
  than the heuristic would derive today? (#1106)
* :func:`apply_tier` — stamp a tier, honouring a manual override.
* :func:`reassess` — re-run the check after classification hardened the
  fragment's voice signals; escalate-only, and only on new evidence.
  Both entry points call it, each as its last mutation before the write
  (#974).
* :func:`edit_added_evidence` — did an in-place *rewrite* introduce a
  privacy signal the old body lacked? (#1136)

Four rules hold across every function here:

**Never auto-downgrade.** Lowering a tier is the only direction that
leaks content, so both the ``--force`` merge and the post-classification
reassess take the *more* restrictive of the two candidates. An operator
who wants a tier relaxed edits the frontmatter by hand.

**Act only on new evidence** (#1105). A tier already on record is
re-litigated only when *this run's* classification made the heuristic
strictly more restrictive than it was on the fragment as loaded —
:func:`reassess` compares its own verdict against the caller's
``baseline`` and returns the fragment untouched when nothing hardened.
That is why the pass needs no notion of *who* wrote the tier (the
frontmatter carries no such provenance): it acts only on evidence that
did not exist when the tier was written, so a settled decision cannot be
overturned by a re-run over unchanged input.

**A changed body is new evidence only where it differs** (#1136). The
rule above assumes the input is unchanged; in-place re-ingest (#673)
breaks that assumption, and :func:`edit_added_evidence` restores it by
asking what the *edit* introduced rather than what the fragment as a
whole now scores. The aggregate comparison :func:`reassess` uses is
necessary but not sufficient here: on a self-authored journal entry the
platform axis pins the verdict at ``intimate`` for any body at all, so an
aggregate gate saturates and can never fire — burying the operator's own
weaker tier on the first benign edit and never letting it back out
(issue #1136). The extra half is
:meth:`~creek.classify.privacy.PrivacyClassifier.body_evidence_tier`,
which reports what the body *alone* proves and so sees past the
saturated axes. Both halves are one-directional: they license an
escalation and never a downgrade, and the merge behind them is still
:func:`escalate`.

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


def needs_retier(
    fragment: Fragment,
    body: str,
    *,
    raw: dict[str, object],
    classifier: PrivacyClassifier,
) -> bool:
    """Return whether *fragment*'s recorded tier is weaker than today's verdict.

    The detector predicate for issue #1106. #974 stopped the pipeline
    stamping self-authored confessional fragments ``personal``, but a
    vault processed before it still carries them, and nothing revisits a
    tier that is already concrete. This is the shape that *proves* such a
    fragment is outstanding::

        escalate(tier_of(f), classify_tier(f, body)) is not tier_of(f)

    **It can only ever fire upwards.** :func:`escalate` picks the higher
    :data:`_ESCALATION_RANK` of the two, so the merge differs from what is
    on disk exactly when the recomputed tier is *more* restrictive. A
    fragment whose heuristic verdict got weaker — a self-authored essay
    recorded ``intimate`` — returns ``False`` and is never handed to a
    writer. That is what makes both the detector and the remediation
    built on it safe against a one-way ratchet: ``privacy_tier`` has no
    way back, so a predicate that could fire downwards would bury content
    permanently.

    **Untiered fragments are deliberately excluded**, and the exclusion is
    load-bearing rather than tidy. ``tier_of`` reports ``unclassified``
    for them, which :data:`_ESCALATION_RANK` puts *below* ``open``, so the
    bare escalate above is ``True`` for every untiered fragment in the
    vault. Those are :func:`needs_tier`'s population (#876) and are
    already counted and already remediated by a plain ``creek classify``;
    folding them in here would make this count a near-copy of that one —
    35,330 of 35,330 on the demo vault — and hide the population this
    predicate exists to surface.

    No I/O, no LLM, no network: :meth:`PrivacyClassifier.classify_tier` is
    keyword and metadata work, so this is free to run on every fragment of
    a 35k-fragment vault.

    Args:
        fragment: The fragment as loaded from disk.
        body: Its markdown body — the classifier scans it for recovery
            keywords, a body-only signal the model does not carry.
        raw: Its frontmatter exactly as read, used to tell "no decision
            yet" from "a decision that is now too weak".
        classifier: The shared :class:`PrivacyClassifier`.

    Returns:
        ``True`` when a re-tier would strictly raise the recorded tier.
    """
    if needs_tier(raw):
        return False
    current = tier_of(fragment)
    candidate = classifier.classify_tier(fragment, content=body)
    return escalate(current, candidate) is not current


def outranks_recorded_tier(fragment: Fragment, candidate: PrivacyTier) -> bool:
    """Return whether *candidate* is strictly more restrictive than *fragment*'s tier.

    The half of :func:`needs_retier` that a caller which has *already*
    computed the heuristic's verdict can reuse, so the engine does not
    classify the same fragment twice per run. It carries no
    :func:`needs_tier` exclusion of its own — the caller applies that,
    because on the engine's path the untiered case is handled by
    :func:`apply_tier` a line later and must not be double-counted.

    Args:
        fragment: The fragment as loaded from disk.
        candidate: The tier
            :meth:`PrivacyClassifier.classify_tier` derived for it.

    Returns:
        ``True`` when merging *candidate* in would raise the recorded tier.
    """
    current = tier_of(fragment)
    return escalate(current, candidate) is not current


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
    baseline: PrivacyTier,
    classifier: PrivacyClassifier,
) -> Fragment:
    """Re-derive the tier after classification, on new evidence only.

    The pre-pass runs *before* the classifier so the per-tier router sees
    a real tier, which means it can only read the axes already on the
    fragment. When classification then hardens the signal — a
    self-authored fragment coming back ``confessional`` + ``conviction``,
    which is
    :meth:`~creek.classify.privacy.PrivacyClassifier.classify_tier`'s
    third INTIMATE trigger — this second look promotes the tier before
    the write, so the harder signal is not thrown away.

    Two independent guards, in this order (#1105):

    1. **The gate.** Compare the heuristic's verdict *now* against
       *baseline*, its verdict on the fragment as loaded. Act only when
       the verdict got **strictly more restrictive**; otherwise this run
       learned nothing new about the fragment's privacy and has no
       standing to disturb a tier already on record.
    2. **The merge.** Escalate-only against ``tier_of(fragment)``, so
       passing the gate is never licence to *lower* what is stored.

    The gate is deliberately "strictly more restrictive" and not "merely
    different". A different verdict includes a *weaker* one, and acting
    on a weakening verdict would let a flip-flopping model raise a tier
    off evidence that pointed the other way — ratcheting a vault tighter
    every run.

    Known limitation, stated plainly. A run that escalates also persists
    the voice axes that justified it, so an operator who then lowers the
    tier by hand keeps it: the next run reads that same voice off disk,
    the baseline moves up with it, and nothing hardens. But if the
    model's verdict is *unstable* — the axes are rewritten weaker on one
    run and confessional again on the next — that later re-hardening is
    genuine new evidence and will raise the tier again. Each flip
    terminates in one step, and the alternative (trusting a tier's
    provenance the frontmatter does not record) is the bug this replaced.

    Args:
        fragment: The classified fragment. Never mutated.
        body: The fragment's markdown body.
        baseline: The heuristic's tier for this fragment *before* this
            run classified anything. Callers compute it on the fragment
            as loaded, so "hardened" means "hardened by this run".
        classifier: The shared :class:`PrivacyClassifier`.

    Returns:
        The fragment at the merged tier — the same object when this run
        hardened nothing, or when the merge changed nothing.
    """
    candidate = classifier.classify_tier(fragment, content=body)
    if escalate(baseline, candidate) is baseline:
        return fragment
    current = tier_of(fragment)
    merged = escalate(current, candidate)
    if merged is current:
        return fragment
    return classifier.enforce_tier(fragment, merged)


def edit_added_evidence(
    fragment: Fragment,
    *,
    old_body: str,
    new_body: str,
    classifier: PrivacyClassifier,
) -> bool:
    """Return whether a rewrite introduced privacy evidence the old body lacked.

    The gate for the in-place-rewrite re-tier (#1136). An automatic
    re-classification may raise a tier already on disk **only on evidence
    the edit itself introduced** — never on authorship or platform, which
    the edit cannot have changed and which the operator has already
    overruled by writing a weaker tier by hand.

    Two disjuncts, either of which is sufficient:

    1. **The aggregate moved.** ``escalate`` the classifier's verdict on
       the old body against its verdict on the new one; a strictly more
       restrictive result means this edit changed the answer. This is the
       same shape as :func:`reassess`'s gate, and it self-maintains: a
       future body-derived rule whose verdict moves is caught here
       without touching this function.
    2. **The body's own claim strengthened.** The aggregate verdict
       saturates whenever a body-independent axis already pins it at
       ``intimate`` — self-authored journal entries, which are the
       *primary* population of the in-place update path (#673). There
       disjunct 1 is silent for every possible edit, so this disjunct
       asks
       :meth:`~creek.classify.privacy.PrivacyClassifier.body_evidence_tier`
       what the body alone proves and fires when the new body proves
       something the old one did not.

    Without disjunct 2 this gate would trade a fail-*closed* bug for a
    fail-*open* one on exactly the fragments the fix is for: a journal
    entry the operator marked ``open``, later rewritten to contain
    recovery material, would keep its ``open`` tier and stay readable at
    an OPEN ceiling. With it, that rewrite still escalates.

    The comparison is one-directional by construction. Both disjuncts
    test for a *strengthening*, so an edit that *removes* evidence
    returns ``False`` — and even a ``True`` only licenses the caller's
    :func:`escalate` merge, which cannot lower anything.

    Residual, stated plainly: an edit that adds intimate material using
    none of :data:`~creek.classify.privacy.RECOVERY_KEYWORDS` does not
    fire either disjunct on a saturated fragment, so it will not escalate
    over an operator's tier. Nor would it have before this gate existed —
    the classifier's verdict is identical on both bodies there, so the
    old code's escalation came purely from the platform axis, which is
    not evidence about the edit. This gate never suppresses an escalation
    that new-body evidence would have produced.

    Pure: no vault, no I/O, no LLM — two keyword/metadata classifications.

    Args:
        fragment: The fragment being rewritten, supplying the authorship,
            platform and voice axes. Never mutated.
        old_body: The body currently on disk, before the rewrite.
        new_body: The body about to replace it.
        classifier: The shared :class:`PrivacyClassifier`.

    Returns:
        ``True`` when the edit introduced evidence that licenses raising
        a tier already on record.
    """
    before = classifier.classify_tier(fragment, content=old_body)
    after = classifier.classify_tier(fragment, content=new_body)
    if escalate(before, after) is not before:
        return True
    old_evidence = classifier.body_evidence_tier(fragment, old_body)
    new_evidence = classifier.body_evidence_tier(fragment, new_body)
    return new_evidence is not None and old_evidence is None
