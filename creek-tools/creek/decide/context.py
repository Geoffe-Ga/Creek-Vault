"""Decision context gathering — pull related context for a decision.

When a Decision note is created, this module gathers contextual
information from related Threads, past Decisions, Praxis notes,
the current Wavelength phase, and frequency affinity.  Context is
assembled into a :class:`DecisionContext` Pydantic model.

Per the Creek Ontology guardrails (Section 12.4), context gathering
NEVER recommends a specific option, weights options by "rationality",
or uses urgency/scarcity/loss-aversion framing.  It surfaces relevant
context and highlights prior similar decisions.
"""

import logging

from pydantic import BaseModel, Field

from creek.models import Decision, Praxis, Thread, WavelengthObservation

logger = logging.getLogger(__name__)


class DecisionContext(BaseModel):
    """Gathered context for a decision.

    Assembles related threads, past decisions, praxis notes,
    current wavelength phase, and frequency affinity into a
    single model for rendering into a Decision note's
    ``## Context`` section.

    Attributes:
        decision_id: The ID of the decision this context belongs to.
        related_threads: Threads semantically related to the decision.
        related_decisions: Past decisions with similar themes.
        related_praxis: Praxis notes relevant to the decision.
        wavelength_phase: The current wavelength phase at decision time.
        frequency_affinity: Frequencies most related to this decision.
    """

    decision_id: str
    related_threads: list[str] = Field(default_factory=list)
    related_decisions: list[str] = Field(default_factory=list)
    related_praxis: list[str] = Field(default_factory=list)
    wavelength_phase: str = ""
    frequency_affinity: list[str] = Field(default_factory=list)


class ContextGatherer:
    """Gather contextual information for a decision.

    Pulls related threads, past decisions, praxis notes, and
    wavelength observations to populate a ``DecisionContext``.
    Matching uses simple keyword overlap on titles and tags.

    Attributes:
        min_tag_overlap: Minimum number of shared tags for a match.
    """

    def __init__(self, min_tag_overlap: int = 1) -> None:
        """Initialise the context gatherer.

        Args:
            min_tag_overlap: Minimum number of shared tags required
                to consider an item related to the decision.
        """
        self.min_tag_overlap = min_tag_overlap

    def gather(
        self,
        decision: Decision,
        threads: list[Thread] | None = None,
        decisions: list[Decision] | None = None,
        praxis_notes: list[Praxis] | None = None,
        observations: list[WavelengthObservation] | None = None,
    ) -> DecisionContext:
        """Gather context for a decision from available primitives.

        Args:
            decision: The decision to gather context for.
            threads: Available threads to search for related ones.
            decisions: Past decisions to search for similar ones.
            praxis_notes: Available praxis notes to check relevance.
            observations: Wavelength observations for current phase.

        Returns:
            A ``DecisionContext`` populated with related items.
        """
        related_threads = self._find_related_threads(decision, threads or [])
        related_decisions = self._find_related_decisions(decision, decisions or [])
        related_praxis = self._find_related_praxis(decision, praxis_notes or [])
        wavelength_phase = self._get_current_phase(observations or [])

        context = DecisionContext(
            decision_id=decision.id,
            related_threads=related_threads,
            related_decisions=related_decisions,
            related_praxis=related_praxis,
            wavelength_phase=wavelength_phase,
            frequency_affinity=list(decision.frequency_context),
        )

        logger.info(
            "Gathered context for decision '%s': "
            "%d thread(s), %d past decision(s), %d praxis note(s)",
            decision.id,
            len(related_threads),
            len(related_decisions),
            len(related_praxis),
        )
        return context

    def _find_related_threads(
        self,
        decision: Decision,
        threads: list[Thread],
    ) -> list[str]:
        """Find threads related to a decision by tag overlap.

        Args:
            decision: The decision to match against.
            threads: Available threads.

        Returns:
            List of related thread IDs.
        """
        decision_tags = set(decision.tags)
        related: list[str] = []
        for thread in threads:
            thread_tags = set(thread.tags)
            overlap = decision_tags & thread_tags
            if len(overlap) >= self.min_tag_overlap:
                related.append(thread.id)
        return related

    def _find_related_decisions(
        self,
        decision: Decision,
        past_decisions: list[Decision],
    ) -> list[str]:
        """Find past decisions related by tag or frequency overlap.

        Excludes the current decision from results.

        Args:
            decision: The current decision.
            past_decisions: All past decisions to search.

        Returns:
            List of related decision IDs.
        """
        decision_tags = set(decision.tags)
        decision_freqs = set(decision.frequency_context)
        related: list[str] = []

        for past in past_decisions:
            if past.id == decision.id:
                continue
            past_tags = set(past.tags)
            past_freqs = set(past.frequency_context)
            tag_overlap = len(decision_tags & past_tags)
            freq_overlap = len(decision_freqs & past_freqs)
            if tag_overlap >= self.min_tag_overlap or freq_overlap > 0:
                related.append(past.id)

        return related

    def _find_related_praxis(
        self,
        decision: Decision,
        praxis_notes: list[Praxis],
    ) -> list[str]:
        """Find praxis notes related by tag or frequency overlap.

        Args:
            decision: The decision to match against.
            praxis_notes: Available praxis notes.

        Returns:
            List of related praxis IDs.
        """
        decision_tags = set(decision.tags)
        decision_freqs = set(decision.frequency_context)
        related: list[str] = []

        for praxis in praxis_notes:
            praxis_tags = set(praxis.tags)
            praxis_freqs = set(praxis.frequency)
            tag_overlap = len(decision_tags & praxis_tags)
            freq_overlap = len(decision_freqs & praxis_freqs)
            if tag_overlap >= self.min_tag_overlap or freq_overlap > 0:
                related.append(praxis.id)

        return related

    def _get_current_phase(
        self,
        observations: list[WavelengthObservation],
    ) -> str:
        """Get the most recent wavelength phase from observations.

        Args:
            observations: Wavelength observations sorted by date.

        Returns:
            The phase string of the most recent observation, or
            empty string if no observations are available.
        """
        if not observations:
            return ""

        most_recent = max(observations, key=lambda o: o.date)
        return str(most_recent.phase)

    def render_context_section(self, context: DecisionContext) -> str:
        """Render a DecisionContext as a markdown ``## Context`` section.

        Produces vault-ready markdown with wikilinks for threads,
        decisions, and praxis notes.

        Args:
            context: The gathered decision context.

        Returns:
            A markdown string suitable for insertion into a Decision note.
        """
        lines: list[str] = ["## Context", ""]

        if context.wavelength_phase:
            lines.append(f"**Current Wavelength Phase:** {context.wavelength_phase}")
            lines.append("")

        if context.frequency_affinity:
            freqs = ", ".join(context.frequency_affinity)
            lines.append(f"**Frequency Affinity:** {freqs}")
            lines.append("")

        if context.related_threads:
            lines.append("### Related Threads")
            for thread_id in context.related_threads:
                lines.append(f"- [[{thread_id}]]")
            lines.append("")

        if context.related_decisions:
            lines.append("### Similar Past Decisions")
            for decision_id in context.related_decisions:
                lines.append(f"- [[{decision_id}]]")
            lines.append("")

        if context.related_praxis:
            lines.append("### Relevant Praxis")
            for praxis_id in context.related_praxis:
                lines.append(f"- [[{praxis_id}]]")
            lines.append("")

        if not any(
            [
                context.related_threads,
                context.related_decisions,
                context.related_praxis,
            ]
        ):
            lines.append("*No related context found yet.*")
            lines.append("")

        return "\n".join(lines)
