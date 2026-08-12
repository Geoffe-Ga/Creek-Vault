"""Structural splitter — the zoom-in re-atomization operator (FEAT-021).

:func:`split` is a pure, deterministic transform on fragments: it carves
a unit into the next finer level (document → section → paragraph →
sentence) and hands the children back. Callers — notably the FEAT-023
orchestrator in :mod:`creek.classify.reatomize` — decide when to invoke
it and how to persist the results.

This package once held a second, zoom-out operator that stitched chat
one-liners into coarser parents (FEAT-022). Issue #1342 retired it: no
production path ever called it, so its config knobs, its CLI value and
three of its orchestrator stop reasons were promises the pipeline could
not keep. ADR-0011 records the decision, and #1457 tracks the coarsening
work should it ever be wanted for real.
"""

from creek.atomize.split import (
    SentenceTokenizer,
    default_sentence_tokenizer,
    split,
)

__all__ = [
    "SentenceTokenizer",
    "default_sentence_tokenizer",
    "split",
]
