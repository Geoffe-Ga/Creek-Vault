"""Confidence-driven re-atomization operators (FEAT-021 / FEAT-022).

This package houses the structural splitter (zoom in, FEAT-021) and the
conversational aggregator (zoom out, FEAT-022). Both operators are
pure, deterministic transforms on fragments: callers (notably FEAT-023)
decide when to invoke them and how to persist the results.
"""

from creek.atomize.aggregate import (
    AggregateLevel,
    AggregationConfig,
    aggregate,
)
from creek.atomize.split import (
    SentenceTokenizer,
    default_sentence_tokenizer,
    split,
)

__all__ = [
    "AggregateLevel",
    "AggregationConfig",
    "SentenceTokenizer",
    "aggregate",
    "default_sentence_tokenizer",
    "split",
]
