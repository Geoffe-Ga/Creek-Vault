"""Embedding-based fragment linker stub.

Provides the ``EmbeddingLinker`` class which will generate vector embeddings
for fragments and find semantic resonances between them.  This module is a
stub — the real implementation comes in issues #28-33.
"""

import logging

from creek.config import EmbeddingsConfig
from creek.models import Fragment

logger = logging.getLogger(__name__)


class EmbeddingLinker:
    """Generate embeddings and find semantic resonances between fragments.

    This is a stub implementation that logs what it would do and returns
    empty results.  The real embedding logic will be added in later issues.

    Attributes:
        config: The embeddings configuration specifying model and threshold.
    """

    def __init__(self, config: EmbeddingsConfig) -> None:
        """Initialise the EmbeddingLinker with the given configuration.

        Args:
            config: Embeddings configuration with model name and
                similarity threshold.
        """
        self.config = config

    def generate_embeddings(self, fragments: list[Fragment]) -> dict[str, list[float]]:
        """Generate vector embeddings for a list of fragments.

        This is a stub that returns an empty dict.  The real implementation
        will use the configured embedding model to vectorise each fragment.

        Args:
            fragments: List of fragments to generate embeddings for.

        Returns:
            A mapping of fragment IDs to their embedding vectors.
            Currently returns an empty dict.
        """
        logger.info(
            "Stub: would generate embeddings for %d fragment(s) using model '%s'",
            len(fragments),
            self.config.model,
        )
        return {}

    def find_resonances(
        self, embeddings: dict[str, list[float]]
    ) -> list[tuple[str, str, float]]:
        """Find semantic resonances between fragments via cosine similarity.

        This is a stub that returns an empty list.  The real implementation
        will compute pairwise similarity and filter by the configured
        threshold.

        Args:
            embeddings: Mapping of fragment IDs to their embedding vectors.

        Returns:
            A list of ``(fragment_id_a, fragment_id_b, similarity)`` tuples
            for each resonance found.  Currently returns an empty list.
        """
        logger.info(
            "Stub: would find resonances among %d embedding(s) with threshold %.2f",
            len(embeddings),
            self.config.similarity_threshold,
        )
        return []
