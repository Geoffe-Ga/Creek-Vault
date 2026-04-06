"""Embedding-based fragment linker using sentence-transformers.

Provides the ``EmbeddingLinker`` class which generates vector embeddings
for fragments and finds semantic resonances between them using cosine
similarity.  Uses locally-run sentence-transformers with disk-based
persistence via numpy compressed archives.
"""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path

    from sentence_transformers import SentenceTransformer as SentenceTransformerType

    from creek.config import EmbeddingsConfig
    from creek.models import Fragment

logger = logging.getLogger(__name__)


def _load_sentence_transformer(
    model_name: str,
    cache_folder: str | None,
) -> SentenceTransformerType:
    """Import and instantiate SentenceTransformer at runtime.

    Lazy import avoids loading torch at module import time.

    Args:
        model_name: HuggingFace model identifier.
        cache_folder: Local directory for caching downloaded models.

    Returns:
        A loaded SentenceTransformer instance.
    """
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, cache_folder=cache_folder)


class EmbeddingLinker:
    """Generate embeddings and find semantic resonances between fragments.

    Uses sentence-transformers to encode fragment text into dense vectors,
    then computes pairwise cosine similarity to discover resonances above
    the configured threshold.

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
        self._model: SentenceTransformerType | None = None

    def load_model(self) -> SentenceTransformerType:
        """Load the sentence-transformer model, caching after first call.

        Returns:
            The loaded SentenceTransformer model instance.
        """
        if self._model is None:
            logger.info("Loading embedding model '%s'", self.config.model)
            self._model = _load_sentence_transformer(
                self.config.model,
                self.config.cache_dir,
            )
        return self._model

    def generate_embedding(self, text: str) -> list[float]:
        """Generate a vector embedding for a single text string.

        Args:
            text: The text to encode.

        Returns:
            A list of floats representing the embedding vector.
        """
        model = self.load_model()
        raw = model.encode(text)
        embedding = np.asarray(raw, dtype=np.float32)
        return [float(x) for x in embedding]

    def generate_embeddings(
        self,
        fragments: list[Fragment],
        *,
        existing_ids: set[str] | None = None,
    ) -> dict[str, list[float]]:
        """Generate vector embeddings for a list of fragments.

        Encodes each fragment's title using the configured
        sentence-transformer model.  Fragments whose IDs appear in
        *existing_ids* are skipped (incremental mode).

        Args:
            fragments: List of fragments to generate embeddings for.
            existing_ids: Fragment IDs to skip (already embedded).

        Returns:
            A mapping of fragment IDs to their embedding vectors.
        """
        if not fragments:
            return {}

        skip = existing_ids or set()
        to_embed = [f for f in fragments if f.id not in skip]

        if not to_embed:
            logger.info("All %d fragment(s) already embedded, skipping", len(fragments))
            return {}

        logger.info(
            "Generating embeddings for %d fragment(s) using model '%s'",
            len(to_embed),
            self.config.model,
        )

        model = self.load_model()
        texts = [f.title for f in to_embed]
        show_progress = logger.isEnabledFor(logging.INFO)
        raw = model.encode(
            texts,
            show_progress_bar=show_progress,
            batch_size=self.config.batch_size,
        )
        vectors = np.asarray(raw, dtype=np.float32)

        return {
            frag.id: [float(x) for x in vectors[i]] for i, frag in enumerate(to_embed)
        }

    def save_embeddings(
        self,
        embeddings: dict[str, list[float]],
        path: Path,
    ) -> None:
        """Persist embeddings to disk as a compressed numpy archive.

        Args:
            embeddings: Mapping of fragment IDs to embedding vectors.
            path: Destination file path (typically ``*.npz``).
        """
        arrays = {k: np.array(v, dtype=np.float32) for k, v in embeddings.items()}
        np.savez_compressed(
            path,
            **arrays,
        )
        logger.info("Saved %d embedding(s) to %s", len(embeddings), path)

    def load_embeddings(self, path: Path) -> dict[str, list[float]]:
        """Load embeddings from a compressed numpy archive.

        Args:
            path: Path to the ``.npz`` file.

        Returns:
            A mapping of fragment IDs to their embedding vectors.

        Raises:
            FileNotFoundError: If *path* does not exist.
        """
        if not path.exists():
            msg = f"Embeddings file not found: {path}"
            raise FileNotFoundError(msg)

        with np.load(path) as data:
            result = {key: data[key].tolist() for key in data.files}
        logger.info("Loaded %d embedding(s) from %s", len(result), path)
        return result

    def find_resonances(
        self,
        embeddings: dict[str, list[float]],
    ) -> list[tuple[str, str, float]]:
        """Find semantic resonances between fragments via cosine similarity.

        Computes pairwise cosine similarity for all embedding pairs and
        returns those above the configured threshold.

        Args:
            embeddings: Mapping of fragment IDs to their embedding vectors.

        Returns:
            A list of ``(fragment_id_a, fragment_id_b, similarity)`` tuples
            for each resonance found.
        """
        ids = list(embeddings.keys())
        if len(ids) < 2:
            return []

        logger.info(
            "Finding resonances among %d embedding(s) with threshold %.2f",
            len(ids),
            self.config.similarity_threshold,
        )

        vectors = np.array([embeddings[id_] for id_ in ids], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-10)
        normalized = vectors / norms
        similarity_matrix = normalized @ normalized.T

        resonances: list[tuple[str, str, float]] = []
        for i, j in itertools.combinations(range(len(ids)), 2):
            sim = float(similarity_matrix[i, j])
            if sim >= self.config.similarity_threshold:
                resonances.append((ids[i], ids[j], sim))

        logger.info("Found %d resonance(s)", len(resonances))
        return resonances
