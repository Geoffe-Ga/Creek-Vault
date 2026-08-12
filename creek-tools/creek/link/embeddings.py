"""Embedding-based fragment linker using sentence-transformers.

Provides the ``EmbeddingLinker`` class which generates vector embeddings
for fragments and finds semantic resonances between them using cosine
similarity.  Uses locally-run sentence-transformers with disk-based
persistence via parquet so per-fragment freshness can be tracked across
runs (INC-006).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

from pydantic import BaseModel

# FragmentLevel is a Literal alias that Pydantic resolves at class-creation
# time when building ``Resonance``'s field schema; moving it under
# TYPE_CHECKING would leave the symbol undefined and crash the import.
from creek.models import (
    FragmentLevel,  # noqa: TC001  # Issue #255: Pydantic runtime resolution
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray
    from sentence_transformers import SentenceTransformer as SentenceTransformerType

    from creek.config import EmbeddingsConfig
    from creek.models import Fragment

_DEFAULT_LEVEL: Final[FragmentLevel] = "document"
"""Fallback level for fragments not present in the supplied lookup map.

When :meth:`EmbeddingLinker.find_resonances` is called without a
``fragments`` argument (the flat-fragment / pre-FEAT-020 contract), every
resonance reports both endpoints at ``"document"`` level — which matches
``Fragment.level``'s own default and keeps the regression test honest.
"""

logger = logging.getLogger(__name__)

EMBEDDINGS_CACHE_FILENAME: Final[str] = "embeddings.parquet"
"""Canonical name for the per-vault embeddings cache (INC-006)."""

EMBEDDINGS_CACHE_DIR: Final[str] = "00-Creek-Meta"
"""Vault subdirectory that holds the embeddings cache."""

_RESONANCE_BLOCK_ROWS: Final[int] = 512
"""Row-block size for chunked resonance similarity (#596).

Similarities are computed one block of rows at a time so peak memory is
``block_rows x N`` floats rather than the full ``N x N`` matrix — keeping
resonance discovery tractable at ≥50k fragments."""


class EmbeddingModelUnavailableError(RuntimeError):
    """The configured sentence-transformer model could not be loaded (GAP-008).

    Raised by :meth:`EmbeddingLinker.load_model` when the underlying
    ``sentence_transformers.SentenceTransformer(...)`` call fails — most
    commonly because the model weights have not been downloaded and the
    host has no network access, but also covers torch/transformers
    import-time failures or a corrupt cache directory. The exception
    message names the requested model so the operator can act without
    rummaging through a stack trace deep inside torch.
    """


class Resonance(BaseModel):
    """A semantic resonance edge between two fragments (FEAT-024).

    Replaces the old ``(fragment_a_id, fragment_b_id, similarity)`` tuple
    so downstream surfaces (synchronicity detection, mining) can read
    the structural level of each endpoint without re-joining against the
    fragment table. The two ``*_level`` fields default to ``"document"``
    so flat-fragment vaults — where no hierarchy has been carved —
    report a stable, predictable value rather than ``None``.

    Attributes:
        fragment_a_id: ID of the first fragment in the resonance.
        fragment_b_id: ID of the second fragment in the resonance.
        similarity: Cosine similarity between the two embedding vectors.
        from_level: Structural level of ``fragment_a_id``'s fragment.
        to_level: Structural level of ``fragment_b_id``'s fragment.
    """

    fragment_a_id: str
    fragment_b_id: str
    similarity: float
    from_level: FragmentLevel = _DEFAULT_LEVEL
    to_level: FragmentLevel = _DEFAULT_LEVEL


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


def purge_fragment_ids_from_cache(
    cache_path: Path,
    fragment_ids: Iterable[str],
    *,
    dry_run: bool = False,
) -> int:
    """Remove rows whose ``fragment_id`` matches *fragment_ids* (GAP-001).

    The purge engine deletes fragments from disk but, before this hook,
    left their cached embedding vectors in
    ``<vault>/00-Creek-Meta/embeddings.parquet``. That broke the
    right-to-be-forgotten contract for intimate-tier content because
    sentence-transformer embeddings are partially invertible. This helper
    is the per-row scrubber the engine now calls.

    A missing cache file is treated as zero rows removed (cache simply
    has not been built yet) rather than an error, so the purge engine
    can call this unconditionally without first probing the filesystem.

    Args:
        cache_path: Embeddings parquet path (typically the result of
            :func:`embeddings_cache_path`).
        fragment_ids: IDs whose rows should be dropped.
        dry_run: When ``True``, count matches without rewriting the file.

    Returns:
        Number of rows actually removed (or that would be removed in a
        dry run). Zero when the file is missing, the id set is empty,
        or no rows match.
    """
    ids_set = set(fragment_ids)
    if not ids_set or not cache_path.exists():
        return 0

    import pyarrow as pa  # lazy: pyarrow lives in the [embeddings] extra
    import pyarrow.parquet as pq

    table = pq.read_table(cache_path)
    ids_in_cache = table["fragment_id"].to_pylist()
    keep_mask = [fid not in ids_set for fid in ids_in_cache]
    removed = len(ids_in_cache) - sum(keep_mask)
    if removed == 0:
        return 0

    if not dry_run:
        keep_array = pa.array(keep_mask, type=pa.bool_())
        pq.write_table(
            table.filter(keep_array),
            cache_path,
            compression="snappy",
        )
        logger.info(
            "Purged %d row(s) from embeddings cache %s",
            removed,
            cache_path,
        )
    return removed


def delete_embeddings_cache(cache_path: Path, *, dry_run: bool = False) -> int:
    """Delete the entire embeddings cache file (GAP-001 vault path).

    ``creek purge vault`` wipes every fragment, so a row-by-row scrub is
    pointless overhead. Deleting the parquet file outright also matches
    the threat model's expectation that nothing of intimate-tier content
    survives a full-vault purge — including the embedding vectors,
    which are partially invertible.

    Args:
        cache_path: Embeddings parquet path.
        dry_run: When ``True``, count rows but leave the file alone.

    Returns:
        Number of rows that were in the file when it was deleted
        (or that would be deleted in a dry run). Zero when the file
        does not exist.
    """
    if not cache_path.exists():
        return 0

    import pyarrow.parquet as pq  # lazy: pyarrow lives in the [embeddings] extra

    row_count = int(pq.read_metadata(cache_path).num_rows)
    if not dry_run:
        cache_path.unlink()
        logger.info(
            "Deleted embeddings cache %s (%d row(s) removed)",
            cache_path,
            row_count,
        )
    return row_count


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


def _hierarchy_index(
    fragments: Mapping[str, Fragment] | None,
) -> tuple[dict[str, frozenset[str]], dict[str, tuple[str, int]]]:
    """Precompute ancestor sets and sibling positions for hierarchy filtering.

    Two derived structures keep the per-pair check in
    :meth:`EmbeddingLinker.find_resonances` O(1) amortised:

    - ``ancestors[fid]`` is the frozen set of every fragment ID reachable
      by walking ``parent_id`` upward from *fid*. Used by the
      ancestor/descendant pair check.
    - ``sibling_positions[child_id] = (parent_id, index)`` records each
      child's slot in its parent's ``child_ids`` list. Built from the
      *parent's* declaration so the index is anchored in the canonical
      sibling order (which the FEAT-021 splitter emits deterministically).

    Args:
        fragments: Mapping of fragment ID to :class:`Fragment`, or
            ``None`` when no hierarchy data is available.

    Returns:
        A tuple ``(ancestors, sibling_positions)``. Both maps are empty
        when *fragments* is ``None`` or empty — suppression then becomes
        a no-op without special-casing in the caller.
    """
    if not fragments:
        return {}, {}

    sibling_positions: dict[str, tuple[str, int]] = {}
    for parent_id, parent in fragments.items():
        for index, child_id in enumerate(parent.child_ids):
            sibling_positions[child_id] = (parent_id, index)

    ancestors: dict[str, frozenset[str]] = {}
    for fid, frag in fragments.items():
        chain: set[str] = set()
        current = frag.parent_id
        # Cycle guard: ``current in chain`` catches a self-referential or
        # circular parent_id link without raising. The vault writer never
        # produces these, but a hand-edited fragment could.
        while current and current not in chain:
            chain.add(current)
            next_frag = fragments.get(current)
            current = next_frag.parent_id if next_frag is not None else None
        ancestors[fid] = frozenset(chain)
    return ancestors, sibling_positions


def _level_lookup(
    ids: list[str],
    fragments: Mapping[str, Fragment] | None,
) -> dict[str, FragmentLevel]:
    """Return a level lookup for every embedded fragment ID.

    Missing or absent entries fall back to ``"document"`` so the
    resulting :class:`Resonance` records carry a meaningful value even
    when the caller has not threaded a fragment map through.

    Args:
        ids: Fragment IDs that appeared in the embedding map.
        fragments: Optional mapping of fragment ID to :class:`Fragment`.

    Returns:
        A mapping of fragment ID to its declared level.
    """
    if not fragments:
        return dict.fromkeys(ids, _DEFAULT_LEVEL)
    return {fid: _level_for(fid, fragments) for fid in ids}


def _level_for(fid: str, fragments: Mapping[str, Fragment]) -> FragmentLevel:
    """Return the level of *fid* in *fragments*, falling back to ``"document"``."""
    frag = fragments.get(fid)
    if frag is None:
        return _DEFAULT_LEVEL
    return frag.level


def _top_k_neighbours(
    row: NDArray[np.float32],
    i: int,
    threshold: float,
    top_k: int,
) -> list[int]:
    """Return the indices of fragment *i*'s top-*top_k* above-*threshold* neighbours.

    Excludes ``i`` itself (cosine self-similarity is 1.0). Uses
    :func:`numpy.argpartition` to select the *top_k* largest among the
    above-threshold candidates in O(c) rather than fully sorting the row, where
    *c* is the candidate count. The returned indices are unordered — the caller
    re-sorts the final edge list into the canonical ``(i, j)`` order.

    Tie-breaking is implementation-defined: ``argpartition`` is not stable, so
    when more than *top_k* candidates share the same similarity, which of the
    tied neighbours fall inside the cap is unspecified (and may differ across
    numpy versions). Edges above the cut are always exact and above threshold.

    Args:
        row: Cosine similarities of fragment *i* against every fragment.
        i: The query fragment's index (excluded from its own neighbours).
        threshold: Minimum similarity for a neighbour to qualify.
        top_k: Maximum neighbours to keep.

    Returns:
        Up to *top_k* neighbour indices, each with ``row[idx] >= threshold``.
    """
    import numpy as np  # lazy: numpy lives in the [embeddings] extra

    candidates = np.nonzero(row >= threshold)[0]
    candidates = candidates[candidates != i]
    if candidates.size == 0:
        return []
    if candidates.size > top_k:
        candidate_sims = row[candidates]
        keep = np.argpartition(candidate_sims, candidates.size - top_k)[
            candidates.size - top_k :
        ]
        candidates = candidates[keep]
    return [int(idx) for idx in candidates]


def _is_hierarchy_suppressed(
    a_id: str,
    b_id: str,
    *,
    ancestors: dict[str, frozenset[str]],
    sibling_positions: dict[str, tuple[str, int]],
    sibling_skip_window: int,
) -> bool:
    """Return whether the pair ``(a_id, b_id)`` is hierarchy-trivial.

    Suppression triggers when *either* fragment is an ancestor of the
    other (via ``parent_id`` chain), *or* both fragments share a parent
    and their indices in that parent's ``child_ids`` differ by at most
    *sibling_skip_window*. A non-positive window disables the sibling
    check; ancestor suppression always applies.

    Args:
        a_id: First fragment ID.
        b_id: Second fragment ID.
        ancestors: Precomputed ancestor sets (see :func:`_hierarchy_index`).
        sibling_positions: Precomputed sibling positions (idem).
        sibling_skip_window: Adjacent-sibling suppression radius (``>= 0``).

    Returns:
        ``True`` if the pair should be dropped from the resonance graph.
    """
    if b_id in ancestors.get(a_id, frozenset()):
        return True
    if a_id in ancestors.get(b_id, frozenset()):
        return True
    if sibling_skip_window <= 0:
        return False
    pos_a = sibling_positions.get(a_id)
    pos_b = sibling_positions.get(b_id)
    if pos_a is None or pos_b is None:
        return False
    parent_a, index_a = pos_a
    parent_b, index_b = pos_b
    if parent_a != parent_b:
        return False
    return abs(index_a - index_b) <= sibling_skip_window


@dataclass(frozen=True, eq=False)
class _ResonanceScan:
    """Everything the pair traversal needs, derived once from the raw inputs.

    Built by :meth:`EmbeddingLinker._prepare_scan` and shared verbatim by
    :meth:`~EmbeddingLinker.find_resonances` and
    :meth:`~EmbeddingLinker.count_resonances` so the two entry points
    cannot diverge on their inputs — including on how they *fail*. The
    ragged-input ``ValueError`` numpy raises while stacking ``vectors``
    happens inside the shared preparation step, so both surfaces raise it
    identically (#1337).

    ``eq=False`` because ``normalized`` is a numpy array: the generated
    ``__eq__`` would raise ``ValueError`` ("truth value of an array … is
    ambiguous") and the generated ``__hash__`` a ``TypeError``, so a frozen
    dataclass would otherwise advertise equality and hashability it cannot
    honour. Identity comparison is the honest contract — a scan is a
    one-shot bundle of derived inputs, never a key or a value to compare.

    Attributes:
        ids: Fragment IDs in the row order of ``normalized``.
        normalized: L2-normalised embedding matrix, so a dot product of
            two rows is their cosine similarity.
        threshold: Minimum cosine similarity for a pair to be emitted.
        ancestors: Precomputed ancestor sets (see :func:`_hierarchy_index`).
        sibling_positions: Precomputed sibling positions (idem).
        levels: Fragment ID to declared structural level, used only when
            materialising :class:`Resonance` records.
        sibling_skip_window: Adjacent-sibling suppression radius.
    """

    ids: list[str]
    normalized: NDArray[np.float32]
    threshold: float
    ancestors: dict[str, frozenset[str]]
    sibling_positions: dict[str, tuple[str, int]]
    levels: dict[str, FragmentLevel]
    sibling_skip_window: int


@dataclass
class _ScanStats:
    """Mutable tally the lazy pair scan writes back to its caller.

    A generator cannot return a value alongside what it yields, so the
    hierarchy-suppression count — which the exact and top-k paths both
    accumulate mid-traversal — travels in this holder instead. The caller
    owns it and reads it only after fully consuming the generator; a
    partial consumer would see a partial tally.

    Attributes:
        suppressed: Above-threshold pairs dropped as hierarchy-trivial
            (ancestor/descendant, or siblings inside the skip window).
    """

    suppressed: int = 0


def _resonances_exact(
    scan: _ResonanceScan,
    stats: _ScanStats,
) -> Iterator[tuple[int, int, float]]:
    """Lazily yield every above-threshold pair ``(i, j, similarity)``, ``i < j``.

    Computes the upper triangle one row-block at a time so peak memory is
    ``block_rows x N`` rather than the full matrix. The laziness is the
    point: :meth:`EmbeddingLinker.count_resonances` consumes this without
    ever holding an edge, which is what keeps counting allocation-free at
    35k fragments (#1337).

    Args:
        scan: Prepared inputs shared with the other entry point.
        stats: Mutable tally; ``suppressed`` is incremented in place.

    Yields:
        ``(i, j, similarity)`` triples in ascending ``(i, j)`` index order,
        where ``i`` and ``j`` index :attr:`_ResonanceScan.ids`.
    """
    import numpy as np  # lazy: numpy lives in the [embeddings] extra

    ids = scan.ids
    n = len(ids)
    for start in range(0, n, _RESONANCE_BLOCK_ROWS):
        stop = min(start + _RESONANCE_BLOCK_ROWS, n)
        block_sims = scan.normalized[start:stop] @ scan.normalized.T
        for local_index, i in enumerate(range(start, stop)):
            row = block_sims[local_index]
            # Upper triangle only: fragment i against each j > i.
            above = np.nonzero(row[i + 1 :] >= scan.threshold)[0]
            for offset in above:
                j = i + 1 + int(offset)
                if _is_hierarchy_suppressed(
                    ids[i],
                    ids[j],
                    ancestors=scan.ancestors,
                    sibling_positions=scan.sibling_positions,
                    sibling_skip_window=scan.sibling_skip_window,
                ):
                    stats.suppressed += 1
                    continue
                yield i, j, float(row[j])
        del block_sims


def _resonances_topk(
    scan: _ResonanceScan,
    stats: _ScanStats,
    *,
    top_k: int,
) -> Iterator[tuple[int, int, float]]:
    """Yield each fragment's *top_k* best above-threshold neighbours.

    Bounds the edge set to O(n·k). Each directed (i -> neighbour) pick is
    folded to its undirected ``(i < j)`` form and de-duplicated, so the
    output is always a subset of the exact path's edges; the picks are then
    sorted into the same ascending ``(i, j)`` order the exact path emits.

    Unlike :func:`_resonances_exact` this is a generator for signature
    parity only — dedup-then-sort is inherently eager, so the full O(n·k)
    pick list is materialised before the first triple is yielded. That
    bound is small enough not to be the #1337 heap problem; pretending to
    stream here would only hide the allocation.

    Args:
        scan: Prepared inputs shared with the other entry point.
        stats: Mutable tally; ``suppressed`` is incremented in place.
        top_k: Maximum neighbours to keep per fragment.

    Yields:
        ``(i, j, similarity)`` triples in ascending ``(i, j)`` index order.
    """
    ids = scan.ids
    n = len(ids)
    seen: set[tuple[int, int]] = set()
    picked: list[tuple[int, int, float]] = []
    for start in range(0, n, _RESONANCE_BLOCK_ROWS):
        stop = min(start + _RESONANCE_BLOCK_ROWS, n)
        block_sims = scan.normalized[start:stop] @ scan.normalized.T
        for local_index, i in enumerate(range(start, stop)):
            row = block_sims[local_index]
            for j in _top_k_neighbours(row, i, scan.threshold, top_k):
                lo, hi = (i, j) if i < j else (j, i)
                if (lo, hi) in seen:
                    continue
                seen.add((lo, hi))
                if _is_hierarchy_suppressed(
                    ids[lo],
                    ids[hi],
                    ancestors=scan.ancestors,
                    sibling_positions=scan.sibling_positions,
                    sibling_skip_window=scan.sibling_skip_window,
                ):
                    stats.suppressed += 1
                    continue
                picked.append((lo, hi, float(row[j])))
        del block_sims
    # Tuples sort lexicographically; (lo, hi) keys are unique (deduped via
    # ``seen``), so this yields ascending (i, j) order without a key.
    picked.sort()
    yield from picked


def _log_scan_result(scan: _ResonanceScan, stats: _ScanStats, count: int) -> None:
    """Log the outcome of a fully-consumed pair scan.

    Args:
        scan: The prepared scan that was traversed.
        stats: The tally the traversal wrote to. Only meaningful once the
            generator has been drained.
        count: Number of resonances the traversal produced.
    """
    if stats.suppressed:
        logger.info(
            "Suppressed %d hierarchy-trivial resonance(s) "
            "(ancestor/descendant or adjacent siblings within %d)",
            stats.suppressed,
            scan.sibling_skip_window,
        )
    logger.info("Found %d resonance(s)", count)


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

        Raises:
            EmbeddingModelUnavailableError: When the underlying
                ``sentence_transformers.SentenceTransformer(...)`` call
                fails (model weights missing and no network, corrupt
                cache, torch import error, …). The message names the
                model and points at the remediation so the operator does
                not have to read a torch stack trace (GAP-008).
        """
        if self._model is None:
            logger.info("Loading embedding model '%s'", self.config.model)
            try:
                self._model = _load_sentence_transformer(
                    self.config.model,
                    self.config.cache_dir,
                )
            except Exception as exc:
                # GAP-008: any underlying-library failure (OSError,
                # RuntimeError, ImportError from torch, …) becomes a
                # typed error so callers can branch on identity rather
                # than parsing third-party messages.
                cache_hint = (
                    f"cache directory {self.config.cache_dir!r}"
                    if self.config.cache_dir
                    else "the default sentence-transformers cache directory"
                )
                msg = (
                    f"sentence-transformers could not load embedding model "
                    f"{self.config.model!r}. The model weights are usually "
                    f"downloaded automatically on first use; this failure "
                    f"typically means the host has no network access on the "
                    f"first run, or the weights in {cache_hint} are corrupt. "
                    f"Remediation: run `creek link --method embeddings` once "
                    f"on a machine with network access to populate the cache, "
                    f"then copy the cache directory to the offline host. "
                    f"Underlying error: {exc!s}"
                )
                raise EmbeddingModelUnavailableError(msg) from exc
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
        fragments: Mapping[str, Fragment] | None = None,
        *,
        sibling_skip_window: int = 2,
    ) -> list[Resonance]:
        """Find semantic resonances between fragments via cosine similarity.

        Computes pairwise cosine similarity for all embedding pairs and
        returns those above the configured threshold. When *fragments*
        is provided, FEAT-024 hierarchy-aware suppression applies:

        - Pairs where one fragment is an ancestor of the other (via the
          ``parent_id`` chain) are dropped — same text at two granularities
          is not a resonance.
        - Pairs sharing a ``parent_id`` whose positions in the parent's
          ``child_ids`` differ by at most *sibling_skip_window* are
          dropped as topically continuous neighbours.

        With no *fragments* mapping (the flat-fragment contract) the
        suppression is inert and behaviour is identical to pre-FEAT-024.

        Callers that only need the edge total should use
        :meth:`count_resonances`, which walks the same traversal without
        building a :class:`Resonance` per pair.

        Args:
            embeddings: Mapping of fragment IDs to their embedding vectors.
            fragments: Optional mapping of fragment ID to :class:`Fragment`
                used for hierarchy-aware suppression and level metadata.
            sibling_skip_window: Adjacent-sibling suppression radius;
                ``0`` disables sibling suppression (ancestor suppression
                still applies).

        Returns:
            A list of :class:`Resonance` records, one per qualifying pair,
            in ascending ``(i, j)`` fragment-index order.
        """
        scan = self._prepare_scan(embeddings, fragments, sibling_skip_window)
        if scan is None:
            return []

        stats = _ScanStats()
        # The comprehension drains the generator, so ``stats.suppressed`` is
        # complete by the time it is logged. A future *partial* consumer of
        # :meth:`_scan_pairs` would see only a partial tally.
        resonances = [
            Resonance(
                fragment_a_id=scan.ids[i],
                fragment_b_id=scan.ids[j],
                similarity=similarity,
                from_level=scan.levels[scan.ids[i]],
                to_level=scan.levels[scan.ids[j]],
            )
            for i, j, similarity in self._scan_pairs(scan, stats)
        ]
        _log_scan_result(scan, stats, len(resonances))
        return resonances

    def count_resonances(
        self,
        embeddings: dict[str, list[float]],
        fragments: Mapping[str, Fragment] | None = None,
        *,
        sibling_skip_window: int = 2,
    ) -> int:
        """Count the resonances :meth:`find_resonances` would return.

        Returns exactly ``len(find_resonances(...))`` for the same inputs,
        including identical hierarchy suppression, discovery strategy and
        failure modes — both entry points derive their inputs from the same
        :meth:`_prepare_scan` and walk the same generator.

        It exists because the count is sometimes all a caller wants.
        ``creek link --method embeddings`` reports the similarity-edge
        total and then discards the edges: nothing in the codebase persists
        a :class:`Resonance`. Materialising one Pydantic model per edge just
        to call :func:`len` on the list costs a measured ~1,017 bytes per
        edge, which extrapolates to tens of gigabytes of heap at the
        documented 35k-fragment scale. This path allocates nothing per edge
        (#1337).

        Args:
            embeddings: Mapping of fragment IDs to their embedding vectors.
            fragments: Optional mapping of fragment ID to :class:`Fragment`
                used for hierarchy-aware suppression.
            sibling_skip_window: Adjacent-sibling suppression radius;
                ``0`` disables sibling suppression (ancestor suppression
                still applies).

        Returns:
            The number of qualifying resonance edges.
        """
        scan = self._prepare_scan(embeddings, fragments, sibling_skip_window)
        if scan is None:
            return 0

        stats = _ScanStats()
        count = sum(1 for _ in self._scan_pairs(scan, stats))
        _log_scan_result(scan, stats, count)
        return count

    def _prepare_scan(
        self,
        embeddings: dict[str, list[float]],
        fragments: Mapping[str, Fragment] | None,
        sibling_skip_window: int,
    ) -> _ResonanceScan | None:
        """Derive the inputs both resonance entry points traverse.

        Shared so :meth:`find_resonances` and :meth:`count_resonances`
        cannot drift on normalisation, hierarchy indexing, the threshold or
        the too-small-to-pair guard — and so a malformed corpus (vectors of
        unequal length) raises from the same place on both (#1337).

        Args:
            embeddings: Mapping of fragment IDs to their embedding vectors.
            fragments: Optional mapping of fragment ID to :class:`Fragment`.
            sibling_skip_window: Adjacent-sibling suppression radius.

        Returns:
            A :class:`_ResonanceScan`, or ``None`` when fewer than two
            embeddings were supplied — no pair can exist, so the caller
            returns its empty answer without logging a traversal.
        """
        ids = list(embeddings.keys())
        if len(ids) < 2:
            return None

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

        ancestors, sibling_positions = _hierarchy_index(fragments)
        return _ResonanceScan(
            ids=ids,
            normalized=normalized,
            threshold=self.config.similarity_threshold,
            ancestors=ancestors,
            sibling_positions=sibling_positions,
            levels=_level_lookup(ids, fragments),
            sibling_skip_window=sibling_skip_window,
        )

    def _scan_pairs(
        self,
        scan: _ResonanceScan,
        stats: _ScanStats,
    ) -> Iterator[tuple[int, int, float]]:
        """Dispatch to the configured pair-discovery strategy.

        ``exact`` (default) emits every above-threshold pair; ``topk`` keeps
        only each fragment's k best neighbours. Both compute similarities one
        row-block at a time via vectorized matmul (never the full NxN matrix)
        and emit pairs in ascending ``(i, j)`` index order.

        Args:
            scan: Prepared inputs from :meth:`_prepare_scan`.
            stats: Mutable tally the chosen strategy writes suppression
                counts into.

        Returns:
            An iterator of ``(i, j, similarity)`` triples.
        """
        if self.config.resonance_method == "topk":
            return _resonances_topk(scan, stats, top_k=self.config.resonance_top_k)
        return _resonances_exact(scan, stats)
