"""Stub Reflection node for the Creek Writing Desk (FEAT-041, #455).

Judges a drafted body and returns a bounded verdict. The skeleton uses a
trivial groundedness heuristic; issue #473 replaces it with the real
reflection logic plus bounded retries and escalation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creek.author.models import EvidenceBundle, ReflectionVerdict


class ReflectionNode:
    """Stub Reflection node that judges a draft against its evidence."""

    def review(
        self,
        body: str,
        evidence: EvidenceBundle,
        rubric: dict[str, float] | None = None,
    ) -> ReflectionVerdict:
        """Return a verdict for *body* given its *evidence*.

        The stub heuristic: a non-empty body grounded in at least one claim
        passes; anything else escalates. It never returns ``REVISE`` — the
        real reflection logic (issue #473) owns the revise/retry loop and will
        score against *rubric*, which the medium contract supplies (FEAT-041).

        Args:
            body: The drafted prose under review.
            evidence: The evidence the draft was rendered from.
            rubric: The medium's per-dimension reflection weights (§8); the
                stub accepts but does not yet score against it.

        Returns:
            ``"PASS"`` when grounded and non-empty, else ``"ESCALATE"``.
        """
        del rubric  # scored by the real reflection node (#473)
        if body.strip() and evidence.claims:
            return "PASS"
        return "ESCALATE"
