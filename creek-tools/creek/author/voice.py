"""Stub Voice agent for the Creek Writing Desk (FEAT-041, #455).

Renders an :class:`~creek.author.models.EvidenceBundle` into draft prose. The
skeleton emits deterministic mock text that simply lists the evidence; issue
#471 replaces this with the real voice-grounded rendering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creek.author.models import EvidenceBundle


class VoiceAgent:
    """Stub Voice agent that turns evidence into a (mock) drafted body."""

    def render(self, query: str, evidence: EvidenceBundle) -> str:
        """Render *evidence* into draft prose for *query*.

        Args:
            query: The originating user query.
            evidence: The aggregated evidence to render.

        Returns:
            A non-empty mock draft body listing each claim.
        """
        lines = [f"# {query} (stub draft)", ""]
        lines.extend(f"- {claim.claim}" for claim in evidence.claims)
        return "\n".join(lines)
