"""Vault-driven linking engine for the ``creek link`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
chosen linker stage (embeddings, temporal, or eddies), and reports the
resulting link count back to the CLI. Honours ``--rebuild`` by deleting
the cached embeddings parquet so the embedding linker recomputes from
scratch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING

from creek.link.eddies import EddyDetector
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
from creek.link.temporal import TemporalLinker
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from creek.config import CreekConfig
    from creek.models import Fragment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkSummary:
    """Counts produced by a single ``creek link`` run.

    Attributes:
        method: Linker that was run (``embeddings`` / ``temporal`` /
            ``eddies``).
        fragment_count: Number of fragments loaded from the vault.
        link_count: Edges or clusters produced (resonances, temporal
            links, or eddy clusters depending on ``method``).
    """

    method: str
    fragment_count: int
    link_count: int


def run_link(
    *,
    vault_path: Path,
    config: CreekConfig,
    method: str,
    rebuild: bool,
) -> LinkSummary:
    """Run a single linker stage against the vault.

    Args:
        vault_path: Vault root.
        config: Loaded Creek configuration.
        method: ``"embeddings"``, ``"temporal"``, or ``"eddies"``.
        rebuild: When ``True`` and ``method == "embeddings"``, delete
            the cached embeddings parquet before recomputing.

    Returns:
        A :class:`LinkSummary` capturing per-method counts.
    """
    cache_path = embeddings_cache_path(vault_path)
    # ``--rebuild`` is documented as "invalidate the embeddings cache",
    # so only act on it for the embeddings linker. Temporal and eddy
    # methods don't own the cache and shouldn't side-effect it — eddy
    # *consumes* the cache, so blowing it away under that method would
    # silently force a recompute the operator didn't ask for.
    if rebuild and method == "embeddings" and cache_path.exists():
        cache_path.unlink()
        logger.info("Removed cached embeddings parquet at %s", cache_path)

    fragments = _load_fragments(vault_path)

    if not fragments:
        return LinkSummary(method=method, fragment_count=0, link_count=0)

    if method == "embeddings":
        link_count = _run_embeddings(
            fragments=fragments,
            config=config,
            cache_path=cache_path,
        )
    elif method == "temporal":
        temporal = TemporalLinker()
        links = temporal.find_temporal_links(
            fragments,
            window_hours=config.linking.temporal_window_hours,
        )
        link_count = len(links)
    else:
        embeddings = _load_or_compute_embeddings(
            fragments=fragments,
            config=config,
            cache_path=cache_path,
        )
        detector = EddyDetector(embeddings=embeddings)
        eddies = detector.detect_eddies(
            fragments,
            min_fragments=config.linking.eddy_min_fragments,
        )
        link_count = len(eddies)

    return LinkSummary(
        method=method,
        fragment_count=len(fragments),
        link_count=link_count,
    )


def _run_embeddings(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
) -> int:
    """Compute embeddings, optionally re-using the on-disk cache.

    Args:
        fragments: Fragments to embed.
        config: Loaded Creek configuration.
        cache_path: Where to read/write the cached embeddings parquet.

    Returns:
        Number of resonance edges discovered.
    """
    embeddings = _load_or_compute_embeddings(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
    )

    # FEAT-024: hierarchy-aware filtering needs both the fragment map
    # (parent/child relations) and the configured sibling skip window.
    fragments_by_id = {f.id: f for f in fragments}
    resonances = EmbeddingLinker(config=config.embeddings).find_resonances(
        embeddings,
        fragments_by_id,
        sibling_skip_window=config.linking.hierarchy_sibling_skip_window,
    )
    return len(resonances)


def _load_or_compute_embeddings(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
) -> dict[str, list[float]]:
    """Return embeddings for *fragments*, hitting the cache when fresh.

    The cache is consulted per-fragment: rows whose ``content_hash``
    matches the current fragment text and whose ``model_name`` matches
    ``config.embeddings.model`` are reused verbatim; everything else
    is recomputed. ``--rebuild`` handling lives upstream (the cache
    file is deleted before this function runs), so a missing file
    naturally degrades to a full recompute.

    Args:
        fragments: Fragments to embed.
        config: Loaded Creek configuration.
        cache_path: Embedding cache path.

    Returns:
        Mapping of fragment ID to embedding vector.
    """
    linker = EmbeddingLinker(config=config.embeddings)

    cached = _safe_load_cache(linker, cache_path)
    fresh_ids = _ids_with_fresh_cache(fragments, cached)

    new_vectors = linker.generate_embeddings(fragments, existing_ids=fresh_ids)

    embeddings: dict[str, list[float]] = {
        frag.id: cached[frag.id].vector for frag in fragments if frag.id in fresh_ids
    }
    embeddings.update(new_vectors)

    _persist_cache(linker, fragments, cached, new_vectors, fresh_ids, cache_path)
    return embeddings


def _safe_load_cache(
    linker: EmbeddingLinker,
    cache_path: Path,
) -> dict[str, CachedEmbedding]:
    """Load the cache while degrading gracefully on corrupted files.

    Args:
        linker: Linker bound to the current embeddings config.
        cache_path: Cache parquet path.

    Returns:
        The cache mapping, or an empty dict if the file is unreadable.
    """
    try:
        return linker.load_cache(cache_path)
    except (OSError, ValueError):
        logger.warning("Cached embeddings unreadable; recomputing")
        return {}


def _ids_with_fresh_cache(
    fragments: list[Fragment],
    cached: dict[str, CachedEmbedding],
) -> set[str]:
    """Return the IDs of fragments whose cache entry is still valid.

    An entry is fresh when its ``content_hash`` matches the current
    fragment text. Model invalidation is already applied by
    :meth:`EmbeddingLinker.load_cache`, so any entry still in ``cached``
    is from the active model.

    Args:
        fragments: All fragments in this run.
        cached: Cache entries loaded from disk.

    Returns:
        Set of fragment IDs that can skip recomputation.
    """
    fresh: set[str] = set()
    for frag in fragments:
        entry = cached.get(frag.id)
        if entry is None:
            continue
        expected_hash = content_hash_for_text(fragment_embedding_text(frag))
        if entry.content_hash == expected_hash:
            fresh.add(frag.id)
    return fresh


def _persist_cache(
    linker: EmbeddingLinker,
    fragments: list[Fragment],
    cached: dict[str, CachedEmbedding],
    new_vectors: dict[str, list[float]],
    fresh_ids: set[str],
    cache_path: Path,
) -> None:
    """Write the merged cache back to disk, degrading gracefully on IO errors.

    Args:
        linker: Linker bound to the current embeddings config.
        fragments: Fragments in this run; defines the IDs we keep.
        cached: Cache entries loaded from disk before recompute.
        new_vectors: Vectors produced this run.
        fresh_ids: Fragment IDs whose cached entry is still valid.
        cache_path: Cache parquet path.
    """
    new_entries = linker.build_cache_entries(fragments, new_vectors)
    entries = {frag.id: cached[frag.id] for frag in fragments if frag.id in fresh_ids}
    entries.update(new_entries)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        linker.save_cache(entries, cache_path)
    except OSError as exc:
        # Disk full / permission denied / read-only volume. Linking
        # itself succeeded — losing the cache only costs a recompute on
        # the next run, so we degrade gracefully instead of crashing.
        logger.warning(
            "Failed to persist embeddings cache to %s: %s",
            cache_path,
            exc,
        )


def _load_fragments(vault_path: Path) -> list[Fragment]:
    """Load every fragment file under ``<vault>/01-Fragments/``.

    Delegates the per-file validation chain to
    :func:`creek.vault.reader.iter_vault_fragments` so the link
    engine, classify engine, and review runner share one definition
    of "is this a Creek fragment?" Linking doesn't surface I/O
    failures to the operator (yet) — they're skipped at DEBUG
    level inside the helper, the same way they were before.

    Args:
        vault_path: Vault root.

    Returns:
        Sorted list of :class:`Fragment` instances.
    """
    records = iter_vault_fragments(vault_path / "01-Fragments")
    return [fragment for _path, fragment, _body, _raw in records]
