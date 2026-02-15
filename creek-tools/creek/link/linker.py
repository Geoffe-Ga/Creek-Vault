"""Linking pipeline orchestrator.

Provides ``LinkingResult`` (a Pydantic model capturing link counts) and
``LinkingPipeline`` which sequences all four linker stages: embeddings,
temporal, threads, and eddies.
"""

import logging
from pathlib import Path

from pydantic import BaseModel

from creek.config import EmbeddingsConfig, LinkingConfig
from creek.link.eddies import EddyDetector
from creek.link.embeddings import EmbeddingLinker
from creek.link.temporal import TemporalLinker
from creek.link.threads import ThreadDetector
from creek.models import Fragment

logger = logging.getLogger(__name__)


class LinkingResult(BaseModel):
    """Result of a linking pipeline run, capturing counts per link type.

    Attributes:
        resonance_count: Number of semantic resonances found.
        temporal_count: Number of temporal proximity links found.
        thread_count: Number of narrative threads detected.
        eddy_count: Number of topic cluster eddies detected.
    """

    resonance_count: int
    temporal_count: int
    thread_count: int
    eddy_count: int


class LinkingPipeline:
    """Orchestrate the full linking pipeline across all four linker stages.

    The pipeline sequences: embeddings -> temporal -> threads -> eddies,
    collecting results from each stage into a ``LinkingResult``.

    Attributes:
        config: Embeddings configuration for the embedding linker.
        linking_config: Linking configuration for temporal window and
            minimum fragment thresholds.
    """

    def __init__(self, config: EmbeddingsConfig, linking_config: LinkingConfig) -> None:
        """Initialise the LinkingPipeline with both configuration objects.

        Args:
            config: Embeddings configuration with model name and
                similarity threshold.
            linking_config: Linking configuration with temporal window
                and minimum fragment counts.
        """
        self.config = config
        self.linking_config = linking_config

    def run(self, fragments: list[Fragment], vault_path: Path) -> LinkingResult:
        """Run all four linking stages in sequence and return the result.

        Stages are executed in order:
        1. Generate embeddings and find resonances
        2. Find temporal proximity links
        3. Detect narrative threads
        4. Detect topic cluster eddies

        Args:
            fragments: List of fragments to process through the pipeline.
            vault_path: Path to the Obsidian vault (used for future I/O).

        Returns:
            A ``LinkingResult`` with counts from each linking stage.
        """
        logger.info(
            "Starting linking pipeline for %d fragment(s) in vault '%s'",
            len(fragments),
            vault_path,
        )

        # Stage 1: Embeddings and resonances
        embedding_linker = EmbeddingLinker(config=self.config)
        embeddings = embedding_linker.generate_embeddings(fragments)
        resonances = embedding_linker.find_resonances(embeddings)

        # Stage 2: Temporal proximity
        temporal_linker = TemporalLinker()
        temporal_links = temporal_linker.find_temporal_links(
            fragments,
            window_hours=self.linking_config.temporal_window_hours,
        )

        # Stage 3: Thread detection
        thread_detector = ThreadDetector()
        threads = thread_detector.detect_threads(fragments)

        # Stage 4: Eddy detection
        eddy_detector = EddyDetector()
        eddies = eddy_detector.detect_eddies(fragments)

        result = LinkingResult(
            resonance_count=len(resonances),
            temporal_count=len(temporal_links),
            thread_count=len(threads),
            eddy_count=len(eddies),
        )

        logger.info(
            "Linking pipeline complete: %d resonances, %d temporal, "
            "%d threads, %d eddies",
            result.resonance_count,
            result.temporal_count,
            result.thread_count,
            result.eddy_count,
        )

        return result

    def add_wikilinks(self, fragment: Fragment, links: list[str]) -> Fragment:
        """Add wikilinks to a fragment's threads list without duplicates.

        Creates a new ``Fragment`` with the links appended to the threads
        list.  Does not mutate the original fragment.

        Args:
            fragment: The fragment to add wikilinks to.
            links: List of wikilink strings (e.g. ``["[[Thread A]]"]``)
                to add to the fragment's threads.

        Returns:
            A new ``Fragment`` with the links added to its threads list.
        """
        existing = set(fragment.threads)
        new_threads = list(fragment.threads)
        for link in links:
            if link not in existing:
                new_threads.append(link)
                existing.add(link)

        return fragment.model_copy(update={"threads": new_threads})
