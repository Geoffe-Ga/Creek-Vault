"""Detector for the unearned ``classification_method: llm`` stamp (#1357).

Before #1330/#1358, a weighted classification whose LLM call failed soft —
whitespace-only body, provider unavailable, transport error, malformed YAML —
was still written to disk as a success:
:func:`~creek.classify.classify_engine._classify_one_weighted` assigned the
empty ``WeightedFragmentClassification()`` wholesale through
:meth:`~creek.classify.weighted.WeightedFragmentClassification.to_legacy` and
reported ``was_skipped=False``. #1358 stopped the writer. It could not repair
the fragments earlier runs had already written, and those are *permanently*
stranded: :func:`~creek.classify.classify_engine._record_if_preserved` reads an
``llm`` stamp as "already paid for", so no ordinary re-run ever revisits them.

:func:`has_unearned_llm_stamp` is the shared detector for that on-disk state.
Two consumers read it — the classify engine, which refuses to honour the stamp
and re-classifies (:mod:`creek.classify.classify_engine`), and ``creek fill``,
which reports the count — so the two can never disagree about which fragments
are poisoned.

**The signature is a conjunction of three facts, and all three are required.**

1. ``classification_method: llm``. ``rules`` and ``manual`` were never written
   by this path, and ``manual`` is operator curation besides.
2. A ``weighted:`` block that is **present and exactly the empty default**.
   Present rules out every legacy and single-pick fragment (they carry
   ``weighted: null``); *exactly the default* rules out any block a model
   actually filled in — including one that names nothing but explains why, or
   reports its own ``overall_confidence``. The check is an equality against
   :data:`_VACUOUS_PROFILE` rather than a hand-written field-by-field scan
   precisely so a dimension added to the model later cannot quietly fall out
   of it and widen the match.
3. Legacy classification equal to that empty profile's *pure collapse*. The
   defective write replaced ``frequency`` and ``wavelength`` wholesale from
   ``to_legacy()``, so a poisoned fragment always reads
   ``frequency.primary: unclassified`` with no secondaries and every
   ``wavelength`` axis ``unclassified`` with ``descriptor: ''``. A fragment
   that still names a frequency survived the bug and is not its victim.

**What is deliberately *not* in the conjunction.** ``voice`` was flattened by
the same write, but nothing guarantees a later pass has not legitimately
filled it back in, and a conjunct another pass can satisfy produces false
*negatives* — fragments left poisoned forever, which is the exact harm this
exists to end. ``classification_reasoning`` is no help either: an
``intimate``-tier fragment persists an empty string there by design
(:data:`~creek.classify.constants.CLASSIFICATION_REASONING_KEY`), so requiring
it would exempt the vault's most sensitive fragments from the heal.

**The residual false positive, stated honestly.** A genuine run whose model
returned parseable YAML naming zero values across every dimension, with
``overall_confidence: 0.0`` and no reasoning preamble, over a fragment whose
prior classification was already empty, is indistinguishable on disk from the
poison. The cost of matching it is one re-classification, which
:meth:`~creek.classify.weighted.WeightedFragmentClassification.merge_onto`
performs non-destructively; the cost of loosening the predicate to avoid it
would be leaving real victims stranded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify.constants import CLASSIFICATION_METHOD_KEY, LLM_METHOD
from creek.classify.weighted import WeightedFragmentClassification

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek.models import Fragment

__all__ = ["has_unearned_llm_stamp"]

_VACUOUS_PROFILE: WeightedFragmentClassification = WeightedFragmentClassification()
"""The empty profile the pre-#1358 soft-failure path persisted verbatim."""

_COLLAPSED_FREQUENCY, _COLLAPSED_WAVELENGTH, _ = _VACUOUS_PROFILE.to_legacy()
"""The legacy fields that empty profile collapses to, derived rather than typed.

Deriving them from :data:`_VACUOUS_PROFILE` keeps the detector pinned to the
defect: if ``to_legacy`` ever stopped flattening a dimension, the predicate
follows it instead of matching a shape the bug no longer produces. The third
element — ``voice`` — is discarded; see the module docstring for why it stays
out of the conjunction.
"""


def has_unearned_llm_stamp(
    fragment: Fragment,
    raw: Mapping[str, object],
) -> bool:
    """Report whether *fragment* claims an LLM classification it never got.

    Args:
        fragment: The fragment as loaded from disk.
        raw: Its frontmatter dict — the authority on
            ``classification_method``, which is a provenance key rather than
            a :class:`~creek.models.Fragment` field.

    Returns:
        ``True`` only when all three signature facts hold together (see the
        module docstring); ``False`` for every legitimately classified
        fragment, including a genuine all-unclassified LLM verdict.
    """
    if raw.get(CLASSIFICATION_METHOD_KEY) != LLM_METHOD:
        return False
    if fragment.weighted != _VACUOUS_PROFILE:
        return False
    return (
        fragment.frequency == _COLLAPSED_FREQUENCY
        and fragment.wavelength == _COLLAPSED_WAVELENGTH
    )
