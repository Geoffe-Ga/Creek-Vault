"""Deterministic stand-ins for the sentence-transformer the grounding guard uses.

The FEAT-032 grounding guard is wired into ``creek draft`` and the MCP
``creek.draft`` tool through
:func:`creek.generate.grounding.default_embedding_fn`, which loads a real
sentence-transformer via :meth:`creek.link.embeddings.EmbeddingLinker.load_model`.
Tests that want to prove the guard is *actually reached* must therefore go
through that same factory rather than injecting a callable of their own —
otherwise they assert nothing about production wiring.

:class:`BagOfWordsEncoder` is the seam: patching ``load_model`` to return
one keeps the whole production path (factory → linker → ``generate_embedding``
→ ``score_draft``) intact while replacing only the model weights with a
hashing bag-of-words vectoriser. Its similarity behaviour is exact and
predictable, which is what lets a test say "this draft *must* fail the
grounding check" instead of "the command exited 0":

* identical text → cosine ``1.0`` (trips ``derivative_upper``),
* disjoint vocabulary → cosine ``0.0`` (trips ``grounding_fraction_lower``),
* half-shared vocabulary → cosine ``0.5`` (inside both bounds).
"""

from __future__ import annotations

import re
import zlib
from typing import Final

_DIMENSIONS: Final[int] = 128
"""Vector width. Comfortably wider than the token vocabulary these tests
use, so distinct words do not collide into the same bucket and blur the
similarity arithmetic the assertions depend on."""

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9']+")
"""Token pattern. Lowercase-only because :meth:`BagOfWordsEncoder.encode`
case-folds first."""


class BagOfWordsEncoder:
    """A hashing bag-of-words vectoriser shaped like a SentenceTransformer.

    Only :meth:`encode` is implemented — that is the entire surface
    :meth:`creek.link.embeddings.EmbeddingLinker.generate_embedding` touches
    for a single string, so a test can substitute this for the real model
    without stubbing anything else in the embedding stack.
    """

    def encode(self, text: str) -> list[float]:
        """Return a deterministic term-frequency vector for *text*.

        Args:
            text: The string to encode.

        Returns:
            A :data:`_DIMENSIONS`-long vector of term frequencies. Bucketing
            uses :func:`zlib.crc32` rather than :func:`hash` because
            ``PYTHONHASHSEED`` randomises string hashing per process, which
            would make similarity scores — and therefore the assertions built
            on them — vary between runs.
        """
        vector = [0.0] * _DIMENSIONS
        for match in _WORD_RE.finditer(text.lower()):
            bucket = zlib.crc32(match.group(0).encode("utf-8")) % _DIMENSIONS
            vector[bucket] += 1.0
        return vector


GROUNDED_SOURCE_BODY: Final[str] = "alpha bravo charlie delta echo foxtrot golf india\n"
"""Source-fragment body every wiring test grounds its drafts against.

The vocabulary across all three bodies below is drawn from the NATO
alphabet minus ``hotel`` and ``xray``, which collide with ``delta`` and
``uniform`` under ``crc32 % 128``. A collision would inflate one term's
weight and skew the cosine arithmetic these constants promise — the exact
score assertions in ``tests/test_draft_grounding_wiring.py`` are what keep
that promise honest, failing loudly rather than drifting silently."""

IN_BOUNDS_DRAFT_BODY: Final[str] = "alpha bravo charlie delta juliett kilo lima mike\n"
"""Draft sharing four of eight tokens with :data:`GROUNDED_SOURCE_BODY`.

Cosine ``4 / sqrt(8 * 8) == 0.5`` — above ``grounding_lower`` (0.30) so the
single paragraph counts as grounded, and below ``derivative_upper`` (0.85)
so it is not flagged as paraphrase. Both default thresholds are cleared."""

UNGROUNDED_DRAFT_BODY: Final[str] = (
    "november oscar papa quebec romeo sierra tango uniform\n"
)
"""Draft sharing no token with :data:`GROUNDED_SOURCE_BODY`.

Cosine ``0.0`` for its only paragraph, so ``grounding_score`` is ``0.0`` —
below the default ``grounding_fraction_lower`` of 0.30. This is the draft
that *must* make ``creek lint --check draft-grounding`` report a finding;
if it does not, the guard never ran."""

BIOGRAPHICAL_DRAFT_BODY: Final[str] = "I grew up around november oscar papa.\n"
"""Draft carrying a first-person biographical claim.

:func:`creek.generate.grounding.scan_biographical_sentences` returns before
embedding anything when no sentence matches
:func:`~creek.generate.grounding.is_biographical_sentence` — so the cohesion
re-check only reaches the embedding model, and only exercises its failure
handling, on a body like this one. ``I grew up`` is one of the conservative
patterns that heuristic matches."""
