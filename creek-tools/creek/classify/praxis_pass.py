"""Pure policy functions for the praxis-potential pass (issue #877).

:attr:`creek.models.Fragment.praxis_potential` shipped with a default of
``none`` and **zero production writers**, so all three consumers that gate
on ``explicit`` — :mod:`creek.generate.decisions`,
:mod:`creek.generate.mining` and :mod:`creek.generate.compost` — were
structurally unreachable: ``04-Praxis`` and ``08-Decisions`` could never
be populated. This module is the shared, side-effect-free policy layer
that both writers — the ``creek classify`` engine and the ``creek
process`` pipeline — call to close that hole:

* :func:`detect` — score a body and return ``explicit`` or ``none``.
* :func:`escalate` — merge two verdicts, never lowering.
* :func:`apply_praxis` — stamp the merged verdict onto a fragment.

Three rules hold across every function here.

**Never lower a verdict.** :func:`escalate` takes the higher of the two
candidates, so a rules re-run over a vault the LLM (or the operator)
already marked ``explicit`` cannot quietly undo that judgment. The pass
is therefore idempotent, which matters because ``creek classify`` is
re-run over the whole vault routinely.

The LLM path reaches the same invariant through the same function:
:func:`creek.classify.llm.parsing._apply_praxis` merges the model's
answer against the fragment's recorded verdict with :func:`escalate`, and
since it is the *only* writer of ``latent`` (see below), :func:`escalate`
is the single chokepoint every producer of this axis passes through.
Guarding a writer against ``none`` alone is **not** sufficient and was
the original bug: ``latent`` is weaker than ``explicit`` too, and this
heuristic cannot repair such a demotion — :func:`detect` re-derives from
the body and can only ever propose ``explicit`` or ``none``, so a verdict
that came from judgment rather than keywords keeps the demotion to disk.

**Precision over recall.** The heuristic is free and runs over every
fragment, so a signal set that over-fires does not fix the bug — it
floods mining, compost and the decisions report with noise, which is
strictly worse than the current silence. Hence :data:`_EXPLICIT_AT` is
2: one *strong* marker (a task checkbox, a ``Decision:`` line), or two
*distinct* moderate first-person commitments. A single bare "I will"
never fires. Scoring is presence-based over distinct patterns rather
than occurrence-counting — see :func:`detect`.

**Never emit ``latent``.** "There is a practice hiding in here that the
author has not named" is a judgment no regular expression can make, and
a heuristic that guessed at it would poison the one signal the LLM path
exists to provide. ``latent`` therefore reaches a fragment only through
:func:`creek.classify.llm.parsing._apply_praxis`.

Known residuals
---------------

* **Preserved fragments need ``--force``.** The classify engine runs this
  pass only on fragments it actually (re-)classifies; the OPS-001 resume
  short-circuit's narrow writer
  (:func:`creek.classify.classify_engine._write_tier_only`) is
  deliberately *not* widened to carry praxis. So a vault already stamped
  ``classification_method: llm``/``manual`` backfills this axis only
  under an explicit, paid ``creek classify --method llm --force``. That
  is the opposite call from the #876 privacy tier, and on purpose: an
  untiered fragment is a live cloud-egress hole, whereas a missing praxis
  verdict is a missing feature, and silently rewriting the praxis axis of
  every already-curated fragment in a mature vault is not something to do
  without being asked. ``creek fill`` surfaces the outstanding count (see
  :func:`creek.cli._hint_praxis_backfill`).
* **The weighted path is heuristic-only.**
  :func:`creek.classify.weighted.classify_weighted` carries its own
  prompt and its own top-level key allow-list, so on that path praxis
  comes from :func:`detect` alone and never from the model.

Security note: these patterns are matched against unbounded,
attacker-influenced fragment bodies. Every one of them is linear-time —
no nested quantifiers — and all are compiled once at import rather than
per fragment, because this table is applied to all 35,330 bodies of the
demo vault on every run.

No vault, no I/O, no LLM: everything here is a pure function of the
fragment and its body.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from creek.models import PraxisPotential

if TYPE_CHECKING:
    from creek.models import Fragment


PRAXIS_POTENTIAL_KEY: Final[str] = "praxis_potential"
"""Frontmatter key carrying a fragment's :class:`~creek.models.PraxisPotential`."""


_STRONG_WEIGHT: Final[int] = 2
"""Score for a marker that is decisive on its own.

A task checkbox or a ``Decision:`` label is not prose *about* action —
it is the author writing action down in a dedicated form. One is enough.
"""

_MODERATE_WEIGHT: Final[int] = 1
"""Score for a first-person commitment, which needs corroboration.

"I will call the dentist" appears in a substantial fraction of every chat
log ever written. Alone it says nothing; alongside a *different* such
marker it starts to look like the fragment is about doing something.
"""

_EXPLICIT_AT: Final[int] = 2
"""Score at or above which :func:`detect` returns ``explicit``.

Equivalently: one strong marker, or two distinct moderate ones. Lowering
this to 1 is the single change that would turn the heuristic from a fix
into a noise generator.
"""


_SENTENCE_START: Final[str] = r"(?:^[ \t]*|(?<=[.!?])[ \t]+)"
"""Anchor matching the start of a line or of a sentence within a line.

Anchoring is what separates a commitment from reported speech. "He said
I should call the dentist and that I will regret it" carries two
first-person lookalikes and would score the full threshold under an
unanchored search; anchored, it scores zero. Real prose also routinely
puts two sentences on one line, so a line-start-only anchor would miss
the commitments that matter — hence the sentence-boundary alternative.

Both branches use ``[ \\t]`` rather than ``\\s`` so neither can consume a
newline and blur one line's anchor into the next.
"""


_APOSTROPHE_CLASS: Final[str] = "['" + chr(0x2019) + "]"
"""Regex character class matching a straight or typographic apostrophe.

Exported chat logs carry both ("I'm" and its curly-quoted twin). The
curly one is built with :func:`chr` rather than pasted so this file stays
pure ASCII — a literal curly quote is indistinguishable from a straight
one in a code review, which is exactly how a pattern silently stops
matching half its inputs.
"""


def _anchored(phrase: str) -> re.Pattern[str]:
    """Compile *phrase* anchored to a line or sentence start.

    Args:
        phrase: A case-insensitive regular-expression fragment naming one
            moderate marker (e.g. ``r"i will\\b"``).

    Returns:
        The compiled pattern, multiline and case-insensitive.
    """
    return re.compile(
        _SENTENCE_START + phrase,
        re.MULTILINE | re.IGNORECASE,
    )


_PRAXIS_SIGNALS: Final[dict[re.Pattern[str], int]] = {
    # --- Strong: the author has written the action down as an artefact. ---
    # A line-initial markdown task checkbox: "- [ ]", "* [x]", indented or
    # not. Mid-line, "see the list - [ ] item" is prose, so the line anchor
    # is load-bearing, and the marker must be blank/x/X — "- [z]" is not a
    # checkbox.
    re.compile(r"^[ \t]*[-*][ \t]*\[[ xX]\][ \t]", re.MULTILINE): _STRONG_WEIGHT,
    # A line-initial label heading a decision or a practice. Line-anchored
    # for the same reason: "The decision: …, was made in April" is
    # narration, and without the anchor every retrospective sentence
    # mentioning a decision would stamp its fragment ``explicit``.
    re.compile(
        r"^[ \t]*(?:decision|praxis|next step|action item)s?[ \t]*:",
        re.MULTILINE | re.IGNORECASE,
    ): _STRONG_WEIGHT,
    # --- Moderate: first-person commitment; needs a distinct companion. ---
    # ``praxis_potential`` records the *owner's* commitments, so each of
    # these is pinned to "I"/"we". Second- and third-person lookalikes
    # ("you should", "it will", "they will") are the single largest class
    # of false positive in a personal corpus full of chat logs, and the
    # trailing ``\b`` also keeps "I shouldn't" from reading as "I should".
    _anchored(r"i should\b"): _MODERATE_WEIGHT,
    _anchored(r"i will\b"): _MODERATE_WEIGHT,
    _anchored(r"i need to\b"): _MODERATE_WEIGHT,
    _anchored(rf"i{_APOSTROPHE_CLASS}m going to\b"): _MODERATE_WEIGHT,
    _anchored(r"next time (?:i|we)\b"): _MODERATE_WEIGHT,
    _anchored(r"from now on\b"): _MODERATE_WEIGHT,
    _anchored(r"the practice is\b"): _MODERATE_WEIGHT,
}
"""Body patterns that evidence praxis, mapped to their weight.

Compiled once at import: this table is applied to every body in the
vault, so a per-call ``re.compile`` is the difference between a free pass
and a measurable one. Keep every pattern linear-time (see the module
docstring's security note).
"""


_RANK: Final[dict[PraxisPotential, int]] = {
    PraxisPotential.NONE: 0,
    PraxisPotential.LATENT: 1,
    PraxisPotential.EXPLICIT: 2,
}
"""Strength ordering for :func:`escalate` — higher wins a merge.

Deliberately a separate table from
:data:`creek.classify.privacy_pass._ESCALATION_RANK`, not a shared
"pick the bigger enum" helper: that one ranks a different enum on a
different question ("which of two candidate privacy labels is the
stronger claim?") and carries an ``unclassified`` sentinel that has to
sort *below* every real tier. ``PraxisPotential`` has no such sentinel —
``none`` is a real verdict, not the absence of one — so folding the two
together would mean explaining away a rank that does not exist here.
"""


def detect(fragment: Fragment, body: str) -> PraxisPotential:
    """Score *body* against the signal table and return a verdict.

    Scoring is **presence-based over distinct patterns**, not
    occurrence-counting: each pattern contributes its weight at most
    once, however often it matches. Two repetitions of one idiom ("I
    should x. I should y.") are a verbal tic, not a second piece of
    evidence, so that body scores 1 and stays ``none``; two *different*
    moderate markers score 2 and reach ``explicit``.

    Never returns ``latent`` — see the module docstring.

    Args:
        fragment: The fragment being scored. Not read: praxis is a
            body-only axis (a title never states a commitment), and the
            parameter exists so this signature stays parallel with
            :func:`apply_praxis` and with
            :meth:`~creek.classify.privacy.PrivacyClassifier.classify_tier`.
        body: The fragment's markdown body.

    Returns:
        ``EXPLICIT`` when the score reaches :data:`_EXPLICIT_AT`,
        otherwise ``NONE``.
    """
    _ = fragment
    score = sum(
        weight for pattern, weight in _PRAXIS_SIGNALS.items() if pattern.search(body)
    )
    if score >= _EXPLICIT_AT:
        return PraxisPotential.EXPLICIT
    return PraxisPotential.NONE


def escalate(
    current: PraxisPotential,
    candidate: PraxisPotential,
) -> PraxisPotential:
    """Return the stronger of *current* and *candidate*.

    The merge is symmetric in its arguments by construction (each rank
    maps to exactly one verdict, so equal ranks mean equal verdicts).
    That matters: an asymmetric merge would let the *caller's* argument
    order decide whether an ``explicit`` fragment gets demoted back to
    ``none`` — the one direction this function exists to make impossible.

    Args:
        current: The verdict already recorded on the fragment.
        candidate: The verdict just derived.

    Returns:
        Whichever verdict ranks higher on ``none < latent < explicit``.
    """
    if _RANK[candidate] > _RANK[current]:
        return candidate
    return current


def apply_praxis(fragment: Fragment, body: str) -> Fragment:
    """Stamp the merged praxis verdict on *fragment*, escalate-only.

    Args:
        fragment: The fragment to mark. Never mutated.
        body: The fragment's markdown body — the signals are body-only,
            and the :class:`~creek.models.Fragment` model does not carry
            the body.

    Returns:
        The fragment carrying the merged verdict — **the same object**
        when nothing escalated, matching
        :func:`creek.classify.privacy_pass.reassess`'s contract. Callers
        detect "did this run mark anything?" by comparing before/after,
        and this runs over every fragment in the vault, so an
        unconditional copy would both blur that signal and allocate a
        fresh model per fragment for nothing.
    """
    current = PraxisPotential(fragment.praxis_potential)
    merged = escalate(current, detect(fragment, body))
    if merged is current:
        return fragment
    # ``.value`` because ``Fragment`` uses ``use_enum_values=True`` but
    # ``model_copy`` bypasses that coercion: consumers compare
    # ``str(frag.praxis_potential) == PraxisPotential.EXPLICIT.value``, and
    # YAML's SafeDumper cannot represent a bare StrEnum member.
    return fragment.model_copy(update={PRAXIS_POTENTIAL_KEY: merged.value})
