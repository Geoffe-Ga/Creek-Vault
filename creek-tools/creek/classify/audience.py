"""Audience-facing classifier — tag whether a fragment was written for readers.

The voice fingerprint (#633) historically inferred "is this audience-facing?"
from one signal: ``source.platform``. That is too lightweight to tell an essay
from a casual ``OPEN`` Discord post — both can share a platform, and a long
structured journal export looks nothing like a one-line chat reply. This module
adds a real **audience axis** (:data:`~creek.models.Audience`:
``audience-facing`` | ``private`` | ``mixed``) by combining several robust,
documented signals rather than a single keyword/platform grep:

* **platform / medium** — published surfaces (essay, Substack) lean
  audience-facing; journals, chats and DMs lean private.
* **explicit writing kind** — :class:`~creek.models.SourceKind.WRITING` set by
  the Substack ingester is a strong audience-facing vote.
* **privacy tier** — ``OPEN`` corroborates audience-facing; ``INTIMATE`` /
  ``PERSONAL`` corroborate private.
* **structure** — headings, block quotations and genuine length are how
  long-form *for readers* differs from a casual blip; very short bodies read as
  private chatter.

The signals are summed into a score and thresholded; the per-signal weights and
thresholds are named constants below so the heuristic is auditable. An optional
LLM-assisted pass (the :class:`AudienceLLMAssist` seam, consistent with
``classify --method llm``) can refine the verdict when the heuristic lands on
``mixed`` — the rules path is the default and needs no provider.

This axis scopes VOICE only: it never gates the knowledge/retrieval corpus.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from creek.models import (
    PrivacyTier,
    SourceKind,
    SourcePlatform,
)

if TYPE_CHECKING:
    from creek.models import Audience, Fragment


# --- Per-signal weights (positive = audience-facing, negative = private) ----

_PLATFORM_SCORE: dict[SourcePlatform, int] = {
    SourcePlatform.ESSAY: 5,
    SourcePlatform.SUBSTACK: 5,
    SourcePlatform.JOURNAL: -3,
    SourcePlatform.CLAUDE: -2,
    SourcePlatform.CHATGPT: -2,
    SourcePlatform.DISCORD: -1,
    SourcePlatform.EMAIL: -1,
}
"""How strongly each platform votes audience-facing (+) or private (-).

Published surfaces (essay, Substack) score above :data:`_AUDIENCE_FACING_AT`
on their own so a deliberately-published short post stays audience-facing even
after the short-body penalty — the platform *is* the audience signal. Unmapped
platforms (markdown notes, documents, OCR, …) contribute ``0`` so the
structural signals decide them rather than a guess."""

_WRITING_KIND_SCORE = 3
"""Bonus when ``source.kind`` is :class:`SourceKind.WRITING` (published)."""

_PRIVACY_SCORE: dict[PrivacyTier, int] = {
    PrivacyTier.OPEN: 1,
    PrivacyTier.PERSONAL: -1,
    PrivacyTier.INTIMATE: -3,
}
"""Privacy tier as a corroborating audience signal (UNCLASSIFIED → 0)."""

_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^>\s+\S", re.MULTILINE)

_STRUCTURE_HEADING_SCORE = 1
"""Bonus when the body carries Markdown section headings."""

_STRUCTURE_BLOCKQUOTE_SCORE = 1
"""Bonus when the body quotes sources (block quotations)."""

_LONG_FORM_WORDS = 300
"""Word count at or above which a body reads as deliberate long-form."""

_LONG_FORM_SCORE = 1
"""Bonus for clearing :data:`_LONG_FORM_WORDS`."""

_SHORT_WORDS = 40
"""Word count below which a body reads as a casual, private blip."""

_SHORT_PENALTY = -2
"""Penalty for a body under :data:`_SHORT_WORDS` words."""

# --- Decision thresholds on the summed score --------------------------------

_AUDIENCE_FACING_AT = 3
"""Score at or above which the verdict is ``audience-facing``."""

_PRIVATE_AT = -2
"""Score at or below which the verdict is ``private``."""


class AudienceLLMAssist(Protocol):
    """Optional LLM refinement seam for the audience classifier.

    Mirrors the ``classify --method llm`` pattern: a provider-backed pass that
    can sharpen the heuristic verdict. Only consulted when the heuristic is
    uncertain (``mixed``), so a confident rules answer never spends a call.
    """

    def refine(
        self,
        fragment: Fragment,
        content: str,
        heuristic: Audience,
    ) -> Audience:
        """Return a (possibly revised) audience for an ambiguous fragment."""
        ...  # pragma: no cover  # Protocol stub


class AudienceClassifier:
    """Assign the :data:`~creek.models.Audience` axis from layered signals.

    Deterministic and idempotent: the same fragment + body always yields the
    same verdict, so re-running ``creek classify`` never churns the axis.

    Attributes:
        llm_assist: Optional :class:`AudienceLLMAssist` consulted only when the
            heuristic verdict is ``mixed``. ``None`` (the default) keeps the
            classifier pure-rules and provider-free.
    """

    def __init__(self, *, llm_assist: AudienceLLMAssist | None = None) -> None:
        """Initialise the classifier.

        Args:
            llm_assist: Optional LLM refinement seam (see
                :class:`AudienceLLMAssist`). Defaults to ``None`` — rules only.
        """
        self.llm_assist = llm_assist

    def score(self, fragment: Fragment, content: str = "") -> int:
        """Return the summed audience score for *fragment* (+ = audience-facing).

        Args:
            fragment: The fragment whose metadata supplies platform / kind /
                privacy signals.
            content: The raw Markdown body, scanned for structure and length.
                Callers must pass it explicitly — :class:`Fragment` does not
                carry the body.

        Returns:
            An integer; positive leans audience-facing, negative leans private.
        """
        total = 0
        total += _PLATFORM_SCORE.get(SourcePlatform(fragment.source.platform), 0)
        if SourceKind(fragment.source.kind) == SourceKind.WRITING:
            total += _WRITING_KIND_SCORE
        total += _PRIVACY_SCORE.get(PrivacyTier(fragment.privacy_tier), 0)
        total += self._structure_score(content)
        return total

    @staticmethod
    def _structure_score(content: str) -> int:
        """Return the structure/length contribution to the audience score."""
        score = 0
        if _HEADING_RE.search(content):
            score += _STRUCTURE_HEADING_SCORE
        if _BLOCKQUOTE_RE.search(content):
            score += _STRUCTURE_BLOCKQUOTE_SCORE
        words = len(content.split())
        if words >= _LONG_FORM_WORDS:
            score += _LONG_FORM_SCORE
        elif words < _SHORT_WORDS:
            score += _SHORT_PENALTY
        return score

    def classify_audience(self, fragment: Fragment, content: str = "") -> Audience:
        """Return the audience axis for *fragment*.

        Thresholds the summed :meth:`score`: at or above
        :data:`_AUDIENCE_FACING_AT` → ``audience-facing``; at or below
        :data:`_PRIVATE_AT` → ``private``; otherwise ``mixed``. When the verdict
        is ``mixed`` and an :class:`AudienceLLMAssist` is configured, the seam
        is consulted for a refined answer.

        Args:
            fragment: The fragment to classify.
            content: Raw Markdown body (see :meth:`score`).

        Returns:
            The assigned :data:`~creek.models.Audience`.
        """
        total = self.score(fragment, content)
        if total >= _AUDIENCE_FACING_AT:
            return "audience-facing"
        if total <= _PRIVATE_AT:
            return "private"
        if self.llm_assist is not None:
            return self.llm_assist.refine(fragment, content, "mixed")
        return "mixed"

    @staticmethod
    def enforce_audience(fragment: Fragment, audience: Audience) -> Fragment:
        """Return a copy of *fragment* with ``audience`` set to *audience*.

        Args:
            fragment: The fragment to update. Not mutated.
            audience: The audience axis to apply.

        Returns:
            A new :class:`Fragment` carrying the assigned audience.
        """
        return fragment.model_copy(update={"audience": audience})

    def classify_and_enforce(self, fragment: Fragment, content: str = "") -> Fragment:
        """Classify *fragment* and return a copy carrying the verdict.

        Args:
            fragment: The fragment to classify and update.
            content: Raw Markdown body (see :meth:`score`).

        Returns:
            A new :class:`Fragment` with its ``audience`` axis set.
        """
        audience = self.classify_audience(fragment, content)
        return self.enforce_audience(fragment, audience)
