"""Sparse-verdict layering for the legacy classification blocks.

A classifier pass produces a *sparse verdict*: it speaks to the axes its
evidence actually covers and stays silent about the rest. Assigning such
a verdict onto a fragment wholesale erases every axis it was silent
about, which is how one defect earned two issue numbers — once against
the weighted path (#1309) and once against the rules path (#1331).
:func:`layer_determined_over` is the single implementation of the merge
rule both paths share, so neither can drift back into the bug alone.

**THE** ``exclude_defaults`` **INVARIANT this rests on.** Every legacy
classification field's default *is* its "not determined" sentinel
(``UNCLASSIFIED`` / ``[]`` / ``""`` / ``None``). Dumping a verdict with
``exclude_defaults=True`` therefore yields exactly the subset of axes the
classifier actually spoke to, and anything it was silent about is simply
absent from the update — leaving the fragment's prior evidence standing.
**A future field whose default is not a "not determined" sentinel breaks
this silently**: such a field would be dumped whenever a pass left it at
some *other* value and omitted whenever the pass agreed with the
non-sentinel default, so a classifier that never considered the axis at
all would start stamping that default over prior evidence again, with no
test failing on the way past. Give any new classification field a
sentinel default, or teach this function about it explicitly.

**Why this module imports nothing from** :mod:`creek`. Every caller sits
inside a delicate import cycle: :mod:`creek.models` defers its import of
:mod:`creek.classify.weighted` all the way to the bottom of the file
(``creek/models.py:1149``, carrying a ``noqa: E402`` and explained at
``creek/models.py:29`` and ``creek/models.py:813``). Importing only
:mod:`typing` and :mod:`pydantic` — never a :mod:`creek` module — takes
this helper out of that reasoning entirely.

To be accurate about how load-bearing that is: it is not the *only*
workable placement. Every caller already imports names straight from
:mod:`creek.models` at module-load time, so defining this above line 1149
there would have worked too. The standalone module is chosen because it
is independently better — single-purpose, trivially unit-testable, and
no new edge into a 1200-line module — with cycle-immunity as a bonus
rather than a necessity.

:class:`~pydantic.BaseModel` is imported at runtime rather than behind
:data:`typing.TYPE_CHECKING` deliberately: third-party imports are not
what the cycle is about, and a guarded import here would be an
unexecutable line in a module small enough that one of those drops it
under the per-file coverage floor.

Callers:

- :meth:`creek.classify.rules.RuleClassifier.classify`, through its
  ``_layer_over_fragment`` step (#1331).
- :meth:`creek.classify.weighted.WeightedFragmentClassification.merge_onto`
  (#1309).
- ``creek.classify.llm.parsing._apply_voice``, the single-pick LLM
  path's voice block (#1331).
- ``creek.classify.llm.calibration._apply_wavelength``, the single-pick
  LLM path's wavelength block (#1421). This one was the last legacy
  writer still rebuilding wholesale; routing it through here was
  entangled with the FEAT-017 ``_biased_enum`` downgrade, which resets
  mode/orientation/dosage on a low self-reported confidence *on
  purpose*. #1421 settled that as "do not adopt the noisy pick" rather
  than "erase what we knew", which this function then delivers for
  free: the downgrade's ``unclassified`` result **is** the sentinel a
  sparse dump omits. The reasoning is recorded on
  ``calibration._BIASED_DIMENSIONS``.

One writer implements the same rule without calling this function, and
that is deliberate rather than an oversight:
``creek.classify.llm.parsing._apply_frequency`` (#1637). It used to
rebuild the frequency block wholesale — a response whose ``frequency:``
block named no ``primary`` replaced a recorded one with the sentinel —
and #1637 closed that. It stayed a *sibling* of this function rather
than becoming a caller because the frequency block breaks the
``exclude_defaults`` invariant above:
:attr:`~creek.models.FrequencyClassification.secondary` is a list, and
its empty default is a **deliberate clear**, not a "not determined"
sentinel. A named primary is supposed to replace the secondaries
wholesale so stale ones from an earlier verdict do not accumulate — the
asymmetry
:meth:`~creek.classify.weighted.WeightedFragmentClassification.merge_onto`
and ``creek.classify.rules.RuleClassifier._layer_over_fragment`` both
spell out. Dumping such a verdict with ``exclude_defaults=True`` would
drop that empty list and let the stale secondaries survive, inverting
the intended behaviour. So ``_apply_frequency`` carries bespoke
key-presence logic: primary layered, secondary replaced. Do not "tidy"
it into a call to this function; the four places that reasoning is
written down (here, that function, ``merge_onto``, and
``_layer_over_fragment``) must stay in sync.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def layer_determined_over(
    *,
    prior: _ModelT,
    determined: _ModelT,
) -> _ModelT:
    """Overlay only the axes *determined* actually decided onto *prior*.

    The merge rule described in this module's docstring: fields of
    *determined* left at their "not determined" sentinel default are
    absent from the update, so the corresponding evidence already on
    *prior* survives. Fields the pass did decide win outright.

    **The arguments are keyword-only on purpose, and it is a safety
    property rather than a style choice.** Both operands have the same
    type, so swapping them typechecks perfectly while inverting the
    merge — the fresh verdict would become the base and the stale
    evidence would overwrite it. On the voice block that silently
    inverts a privacy control, and no gate in this repo would catch it.
    Naming the roles at every call site is the only thing that can.

    Deliberately generic over :class:`~pydantic.BaseModel` rather than
    typed to the three classification models, because it states
    something about merging sparse verdicts, not something about those
    models. That generality is also its one sharp edge: it is only
    *sound* for a model whose every default is a "not determined"
    sentinel. :class:`~creek.models.Fragment` itself does not qualify —
    ``voice_weight`` defaults to ``1.0``, a real value and not an
    absence — so do not reach for this to merge whole fragments.

    Args:
        prior: The block already on record — whatever a previous run,
            the rule pass, or the operator established. Never mutated.
        determined: This pass's verdict for the same block, carrying a
            sentinel default on every axis it did not decide.

    Returns:
        A copy of *prior* with the non-default fields of *determined*
        applied. An entirely default *determined* yields an unchanged
        copy of *prior*.
    """
    return prior.model_copy(update=determined.model_dump(exclude_defaults=True))
