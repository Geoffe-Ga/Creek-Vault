"""Embedding-based fragment linker using sentence-transformers.

Provides the ``EmbeddingLinker`` class which generates vector embeddings
for fragments and finds semantic resonances between them using cosine
similarity.  Uses locally-run sentence-transformers with disk-based
persistence via parquet so per-fragment freshness can be tracked across
runs (INC-006).
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

from tqdm import tqdm

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sentence_transformers import SentenceTransformer as SentenceTransformerType

    from creek.config import EmbeddingsConfig
    from creek.models import Fragment

logger = logging.getLogger(__name__)

EMBEDDINGS_CACHE_FILENAME: Final[str] = "embeddings.parquet"
"""Canonical name for the per-vault embeddings cache (INC-006)."""

EMBEDDINGS_CACHE_DIR: Final[str] = "00-Creek-Meta"
"""Vault subdirectory that holds the embeddings cache."""


@dataclass(frozen=True)
class CachedEmbedding:
    """A persisted embedding row with the metadata needed to revalidate it.

    Attributes:
        fragment_id: Stable ID of the embedded fragment.
        content_hash: SHA-256 of the text that was embedded; a mismatch
            against the current fragment text triggers a recompute.
        model_name: Sentence-transformer model identifier the vector was
            produced with. Entries for other models are invalidated on
            load so a model swap forces a full recompute.
        vector: The embedding vector itself.
        computed_at: UTC timestamp at which the vector was produced.
    """

    fragment_id: str
    content_hash: str
    model_name: str
    vector: list[float]
    computed_at: datetime


def content_hash_for_text(text: str) -> str:
    """Return a stable SHA-256 hex digest of the embedding source text.

    Args:
        text: The exact string the embedding will (or did) consume.

    Returns:
        Lowercase 64-character hex digest used as the freshness key.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fragment_embedding_text(fragment: Fragment) -> str:
    """Return the canonical text encoded by the embedding model.

    Centralising this keeps the freshness hash and the embedding input
    in lockstep — if the embedding text ever expands beyond the title,
    the hash automatically follows.

    Args:
        fragment: The fragment whose embedding text is requested.

    Returns:
        The exact string passed to the sentence-transformer.
    """
    return fragment.title


def embeddings_cache_path(vault_path: Path) -> Path:
    """Return the canonical embeddings parquet path inside a vault.

    Args:
        vault_path: Vault root directory.

    Returns:
        ``<vault>/00-Creek-Meta/embeddings.parquet``.
    """
    return vault_path / EMBEDDINGS_CACHE_DIR / EMBEDDINGS_CACHE_FILENAME


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

    return cast(
        "SentenceTransformerType",
        SentenceTransformer(model_name, cache_folder=cache_folder),
    )


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
        import numpy as np  # lazy: numpy lives in the [embeddings] extra

        raw = self.load_model().encode(text)
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

        import numpy as np  # lazy: numpy lives in the [embeddings] extra

        model = self.load_model()
        texts = [fragment_embedding_text(f) for f in to_embed]
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

    def save_cache(
        self,
        entries: Mapping[str, CachedEmbedding],
        path: Path,
    ) -> None:
        """Persist freshness-aware cache entries as a parquet file.

        Each entry carries the fragment ID, content hash, model name,
        embedding vector, and computation timestamp so a subsequent load
        can revalidate per fragment rather than treating the file as
        opaque.

        Args:
            entries: Mapping of fragment ID to :class:`CachedEmbedding`.
            path: Destination parquet path; parent dirs must already exist.
        """
        import pyarrow as pa  # lazy: pyarrow lives in the [embeddings] extra
        import pyarrow.parquet as pq

        rows = list(entries.values())
        table = pa.table(
            {
                "fragment_id": pa.array(
                    [row.fragment_id for row in rows],
                    type=pa.string(),
                ),
                "content_hash": pa.array(
                    [row.content_hash for row in rows],
                    type=pa.string(),
                ),
                "model_name": pa.array(
                    [row.model_name for row in rows],
                    type=pa.string(),
                ),
                "embedding": pa.array(
                    [row.vector for row in rows],
                    type=pa.list_(pa.float32()),
                ),
                "computed_at": pa.array(
                    [row.computed_at for row in rows],
                    type=pa.timestamp("us", tz="UTC"),
                ),
            },
        )
        pq.write_table(table, path, compression="snappy")
        logger.info("Saved %d embedding cache row(s) to %s", len(rows), path)

    def load_cache(self, path: Path) -> dict[str, CachedEmbedding]:
        """Load freshness-aware cache entries from a parquet file.

        Entries whose ``model_name`` does not match the current
        ``config.model`` are dropped — a model swap fully invalidates
        the cache so the next run recomputes from scratch.

        Args:
            path: Parquet file produced by :meth:`save_cache`.

        Returns:
            Mapping of fragment ID to :class:`CachedEmbedding`. Empty if
            the file does not exist (treated as a cache miss so the
            engine can fall through to a full recompute without raising).
        """
        import pyarrow.parquet as pq  # lazy: pyarrow lives in [embeddings]

        if not path.exists():
            return {}

        data = pq.read_table(path).to_pylist()
        active_model = self.config.model
        result: dict[str, CachedEmbedding] = {}
        skipped = 0
        for row in data:
            if row["model_name"] != active_model:
                skipped += 1
                continue
            computed_at = row["computed_at"]
            if computed_at.tzinfo is None:
                computed_at = computed_at.replace(tzinfo=UTC)
            result[row["fragment_id"]] = CachedEmbedding(
                fragment_id=row["fragment_id"],
                content_hash=row["content_hash"],
                model_name=row["model_name"],
                vector=[float(x) for x in row["embedding"]],
                computed_at=computed_at,
            )
        if skipped:
            logger.info(
                "Discarded %d cached embedding(s) from other model(s); "
                "active model is '%s'",
                skipped,
                active_model,
            )
        logger.info("Loaded %d embedding cache row(s) from %s", len(result), path)
        return result

    def build_cache_entries(
        self,
        fragments: list[Fragment],
        vectors: Mapping[str, list[float]],
    ) -> dict[str, CachedEmbedding]:
        """Pair freshly-computed vectors with their freshness metadata.

        Args:
            fragments: Fragments whose embeddings were just computed.
            vectors: Mapping of fragment ID to embedding vector for the
                fragments that were actually re-encoded this run.

        Returns:
            Mapping of fragment ID to :class:`CachedEmbedding` covering
            every fragment present in ``vectors``.
        """
        computed_at = datetime.now(tz=UTC)
        by_id = {f.id: f for f in fragments}
        return {
            frag_id: CachedEmbedding(
                fragment_id=frag_id,
                content_hash=content_hash_for_text(
                    fragment_embedding_text(by_id[frag_id]),
                ),
                model_name=self.config.model,
                vector=vector.copy(),
                computed_at=computed_at,
            )
            for frag_id, vector in vectors.items()
        }

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

        import numpy as np  # lazy: numpy lives in the [embeddings] extra

        vectors = np.array([embeddings[id_] for id_ in ids], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-10)
        normalized = vectors / norms
        similarity_matrix = normalized @ normalized.T

        resonances: list[tuple[str, str, float]] = []
        # OPS-004: pairwise loop is O(N²); on a 10k-fragment vault this
        # is ~50M iterations and runs for minutes. Show tqdm in TTYs and
        # stay silent in non-interactive runs (CI logs, pipes).
        n_pairs = len(ids) * (len(ids) - 1) // 2
        pair_iter = tqdm(
            itertools.combinations(range(len(ids)), 2),
            total=n_pairs,
            desc="Resonances",
            unit="pair",
            disable=not sys.stderr.isatty(),
        )
        for i, j in pair_iter:
            sim = float(similarity_matrix[i, j])
            if sim >= self.config.similarity_threshold:
                resonances.append((ids[i], ids[j], sim))

        logger.info("Found %d resonance(s)", len(resonances))
        return resonances
