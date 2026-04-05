"""Decision support pipeline — orchestrate detection and context gathering.

Provides :class:`DecisionResult` (a Pydantic model capturing decision
support counts) and :class:`DecisionPipeline` which sequences decision
detection and context gathering for a set of fragments.
"""

import logging

from pydantic import BaseModel

from creek.decide.context import ContextGatherer, DecisionContext
from creek.decide.detector import DecisionDetector
from creek.models import Decision, Fragment, Praxis, Thread, WavelengthObservation

logger = logging.getLogger(__name__)


class DecisionResult(BaseModel):
    """Result of a decision support pipeline run.

    Attributes:
        fragments_scanned: Number of fragments scanned for decisions.
        decisions_detected: Number of decision-relevant fragments found.
        decisions_created: Number of Decision notes created.
        contexts_gathered: Number of context sections generated.
    """

    fragments_scanned: int = 0
    decisions_detected: int = 0
    decisions_created: int = 0
    contexts_gathered: int = 0


class DecisionPipeline:
    """Orchestrate decision detection and context gathering.

    The pipeline sequences: detection -> decision creation ->
    context gathering, producing a :class:`DecisionResult` with counts.

    Attributes:
        detector: The decision detector for scanning fragments.
        gatherer: The context gatherer for assembling related items.
    """

    def __init__(
        self,
        threshold: int = 2,
        min_tag_overlap: int = 1,
    ) -> None:
        """Initialise the pipeline with detection and gathering config.

        Args:
            threshold: Minimum score for decision detection.
            min_tag_overlap: Minimum shared tags for context matching.
        """
        self.detector = DecisionDetector(threshold=threshold)
        self.gatherer = ContextGatherer(min_tag_overlap=min_tag_overlap)

    def run(
        self,
        fragments: list[Fragment],
        contents: list[str] | None = None,
        threads: list[Thread] | None = None,
        decisions: list[Decision] | None = None,
        praxis_notes: list[Praxis] | None = None,
        observations: list[WavelengthObservation] | None = None,
    ) -> tuple[DecisionResult, list[Decision], list[DecisionContext]]:
        """Run the full decision support pipeline.

        Stages:
            1. Scan fragments for decision-relevant content
            2. Create draft Decision notes for detected fragments
            3. Gather context for each new decision

        Args:
            fragments: Fragments to scan for decisions.
            contents: Optional parallel list of content strings.
            threads: Available threads for context gathering.
            decisions: Existing decisions for context matching.
            praxis_notes: Praxis notes for context matching.
            observations: Wavelength observations for current phase.

        Returns:
            A tuple of (result, new_decisions, contexts) where
            result contains aggregate counts, new_decisions is the
            list of created Decision models, and contexts is the
            list of gathered DecisionContext models.
        """
        logger.info(
            "Starting decision pipeline for %d fragment(s)",
            len(fragments),
        )

        # Stage 1: Detection
        relevant = self.detector.detect(fragments, contents)

        # Stage 2: Decision creation
        new_decisions: list[Decision] = []
        for fragment in relevant:
            decision = self.detector.create_decision(fragment)
            new_decisions.append(decision)

        # Stage 3: Context gathering
        all_decisions = list(decisions or []) + new_decisions
        contexts: list[DecisionContext] = []
        for decision in new_decisions:
            context = self.gatherer.gather(
                decision=decision,
                threads=threads,
                decisions=all_decisions,
                praxis_notes=praxis_notes,
                observations=observations,
            )
            contexts.append(context)

        result = DecisionResult(
            fragments_scanned=len(fragments),
            decisions_detected=len(relevant),
            decisions_created=len(new_decisions),
            contexts_gathered=len(contexts),
        )

        logger.info(
            "Decision pipeline complete: %d detected, %d created, %d contexts gathered",
            result.decisions_detected,
            result.decisions_created,
            result.contexts_gathered,
        )

        return result, new_decisions, contexts
