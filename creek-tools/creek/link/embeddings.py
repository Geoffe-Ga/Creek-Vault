"""Embedding-based fragment linker using bag-of-words cosine similarity.

Provides the ``EmbeddingLinker`` class which generates lightweight
term-frequency vectors from fragment metadata (title, tags,
emotional_texture) and finds semantic resonances via cosine similarity.

No external ML dependencies are required — embeddings are pure-Python
bag-of-words vectors suitable for the rule-based pipeline stage.
"""

import logging
import math
import re
from collections import Counter

from creek.config import EmbeddingsConfig
from creek.models import Fragment

logger = logging.getLogger(__name__)

_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenise(text: str) -> list[str]:
    """Split text into lowercase alphanumeric tokens.

    Args:
        text: Raw text to tokenise.

    Returns:
        List of lowercase tokens extracted from the text.
    """
    return _WORD_PATTERN.findall(text.lower())


def _fragment_text(fragment: Fragment) -> str:
    """Extract linkable text from a fragment's metadata fields.

    Combines the fragment's title, tags, and emotional texture into a
    single string for vectorisation.

    Args:
        fragment: The fragment to extract text from.

    Returns:
        A space-separated string of all textual metadata.
    """
    parts: list[str] = [fragment.title]
    parts.extend(fragment.tags)
    parts.extend(fragment.emotional_texture)
    return " ".join(parts)


def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Compute cosine similarity between two sparse term-frequency vectors.

    Args:
        vec_a: First sparse vector (token -> frequency).
        vec_b: Second sparse vector (token -> frequency).

    Returns:
        Cosine similarity in the range ``[0.0, 1.0]``.  Returns ``0.0``
        if either vector is empty.
    """
    common_keys = set(vec_a) & set(vec_b)
    if not common_keys:
        return 0.0

    dot = sum(vec_a[k] * vec_b[k] for k in common_keys)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))

    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0

    return dot / (mag_a * mag_b)


class EmbeddingLinker:
    """Generate bag-of-words embeddings and find semantic resonances.

    Uses term-frequency vectors built from fragment metadata (title,
    tags, emotional_texture) to compute pairwise cosine similarity.
    Fragment pairs exceeding the configured similarity threshold are
    returned as resonances.

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

    def generate_embeddings(
        self, fragments: list[Fragment]
    ) -> dict[str, dict[str, float]]:
        """Generate bag-of-words vectors for a list of fragments.

        Each fragment is converted to a sparse term-frequency vector
        keyed by lowercase tokens extracted from its metadata fields.

        Args:
            fragments: List of fragments to generate embeddings for.

        Returns:
            A mapping of fragment IDs to their sparse term-frequency
            vectors (token -> count).
        """
        logger.info(
            "Generating bag-of-words embeddings for %d fragment(s)",
            len(fragments),
        )
        result: dict[str, dict[str, float]] = {}
        for fragment in fragments:
            text = _fragment_text(fragment)
            tokens = _tokenise(text)
            counts = Counter(tokens)
            result[fragment.id] = {k: float(v) for k, v in counts.items()}
        return result

    def find_resonances(
        self, embeddings: dict[str, dict[str, float]]
    ) -> list[tuple[str, str, float]]:
        """Find semantic resonances between fragments via cosine similarity.

        Computes pairwise cosine similarity between all embedding pairs
        and returns those exceeding the configured threshold.

        Args:
            embeddings: Mapping of fragment IDs to their sparse
                term-frequency vectors.

        Returns:
            A list of ``(fragment_id_a, fragment_id_b, similarity)``
            tuples for each resonance found, sorted by similarity
            descending.
        """
        ids = sorted(embeddings.keys())
        resonances: list[tuple[str, str, float]] = []

        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                sim = _cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim >= self.config.similarity_threshold:
                    resonances.append((id_a, id_b, sim))

        resonances.sort(key=lambda r: r[2], reverse=True)

        logger.info(
            "Found %d resonance(s) among %d embedding(s) with threshold %.2f",
            len(resonances),
            len(embeddings),
            self.config.similarity_threshold,
        )
        return resonances
